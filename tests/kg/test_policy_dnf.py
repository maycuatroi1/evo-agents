"""Who reads an element some derivations reveal (``evo_agents.schema.policy.PolicyDNF``) and how facts merge their
strength (``evo_agents.kg.assembly.stronger``).

The algebra is checked on every policy of a small universe of atoms: both operations commutative, associative and
idempotent, the normal form an antichain, and ``allows`` a homomorphism into the booleans. Then the three faults of the
single label the policy replaces: two derivations at (1, 0) and (0, 1) no longer show to a reader at (0, 0), two chats
no longer hide an element from a member of one, and the props of a node are those of a branch its reader reads.
"""

from __future__ import annotations

import itertools
import random
from types import SimpleNamespace

from evo_agents.kg.assembly import STATUS_RANK, Facts, _strongest, assemble, strength
from evo_agents.kg.graph_model import Graph, graph_tables, read_as, search_rows, within
from evo_agents.kg.snapshot import Snapshot, edge_dict, edge_row, node_dict, node_row
from evo_agents.schema.policy import TOP_AUDIENCE, Label, PolicyDNF, covers

C5, C6 = "chat:5", "chat:6"
AUDIENCES = [frozenset(), frozenset({C5}), frozenset({C6}), frozenset({C5, C6}), TOP_AUDIENCE]
ATOMS = [
    Label(level, location, integrity, frozenset(), audience)
    for level, location, integrity, audience in itertools.product((0, 1), (0, 1), "TU", AUDIENCES)
]
READERS = [
    (level, location, holds)
    for level, location, holds in itertools.product((0, 1), (0, 1), [frozenset(), *AUDIENCES[1:4]])
]
SEEDED = random.Random(20261124)
POLICIES = [PolicyDNF.never(), PolicyDNF.always()] + [
    PolicyDNF.of(*SEEDED.sample(ATOMS, SEEDED.randint(1, 4))) for _ in range(40)
]


def readers_of(policy: PolicyDNF) -> set:
    return {r for r in READERS if policy.allows(*r)}


def test_both_operations_are_commutative_associative_and_idempotent():
    for p, q in itertools.product(POLICIES, repeat=2):
        assert p.disjoin(q) == q.disjoin(p) and p.conjoin(q) == q.conjoin(p)
    for p in POLICIES:
        assert p.disjoin(p) == p and p.conjoin(p) == p
    for p, q, r in itertools.islice(itertools.product(POLICIES, repeat=3), 0, None, 7):
        assert p.disjoin(q).disjoin(r) == p.disjoin(q.disjoin(r))
        assert p.conjoin(q).conjoin(r) == p.conjoin(q.conjoin(r))


def test_the_normal_form_is_an_antichain_independent_of_the_order_of_its_atoms():
    for p in POLICIES:
        assert not any(a is not b and covers(a, b) for a in p for b in p)
        assert PolicyDNF.of(*reversed(p.atoms)) == p
    # The same readable dimensions are one atom, trusted when one of them is.
    assert PolicyDNF.of(Label(1, 0, "U", frozenset()), Label(1, 0, "T", frozenset())).atoms == (
        Label(1, 0, "T", frozenset()),
    )


def test_allows_is_a_homomorphism_of_both_operations_and_their_units():
    for p, q in itertools.product(POLICIES, repeat=2):
        assert readers_of(p.disjoin(q)) == readers_of(p) | readers_of(q)
        assert readers_of(p.conjoin(q)) == readers_of(p) & readers_of(q)
    for p in POLICIES:
        assert p.disjoin(PolicyDNF.never()) == p and p.conjoin(PolicyDNF.always()) == p
        assert p.conjoin(PolicyDNF.never()) == PolicyDNF.never()
    assert readers_of(PolicyDNF.never()) == set() and readers_of(PolicyDNF.always()) == set(READERS)


def test_conjoin_of_single_atoms_is_their_join():
    for a, b in itertools.product(ATOMS, repeat=2):
        assert PolicyDNF.of(a).conjoin(PolicyDNF.of(b)).atoms == (a.join(b),)


def test_two_chats_show_to_a_member_of_either_and_two_dimensions_never_meet_below_both():
    five, six = (
        PolicyDNF.of(Label(1, 0, "T", frozenset(), frozenset({C5}))),
        PolicyDNF.of(Label(1, 0, "T", frozenset(), frozenset({C6}))),
    )
    either = five.disjoin(six)
    assert either.allows(1, 0, frozenset({C5})) and either.allows(1, 0, frozenset({C6}))
    assert not either.allows(1, 0, frozenset()) and not either.allows(0, 0, frozenset({C5, C6}))
    crossed = PolicyDNF.of(Label(1, 0, "T", frozenset()), Label(0, 1, "T", frozenset()))
    assert not crossed.allows(0, 0) and crossed.allows(1, 0) and crossed.allows(0, 1)
    # The projects of a label are a condition too: a reader holds the project it reads.
    held = PolicyDNF.of(Label(1, 0, "trusted", frozenset({"chat:5"})))
    assert held.allows(1, 0, frozenset({"chat:5"})) and not held.allows(1, 0, frozenset())


def test_facts_merge_by_the_pair_of_rank_and_confidence_whole():
    weak, guess = {"status": "resolved", "conf": 0.3}, {"status": "proposed", "conf": 0.9}
    for first, second in ((weak, guess), (guess, weak)):
        held = dict(first)
        _strongest(held, second)
        assert held == weak  # never resolved at 0.9: the guess does not lift it over the floor
    facts = [{"status": s, "conf": c} for s in STATUS_RANK for c in (0.2, 0.5, 0.9)]
    for order in (facts, list(reversed(facts)), SEEDED.sample(facts, len(facts))):
        held = dict(order[0])
        for f in order[1:]:
            _strongest(held, f)
        assert held == max(facts, key=strength)


class _Rec(SimpleNamespace):
    source = "chats"


def _node(nid: str, unit: str, props: dict, kind: str = "Code") -> dict:
    return {
        "t": "node",
        "id": nid,
        "kind": kind,
        "name": nid,
        "props": props,
        "status": "parsed",
        "conf": 1.0,
        "units": [unit],
        "needs": [],
    }


def assembled(units: dict[str, Label], facts: list[dict]) -> Graph:
    found = Facts(kinds=None)
    for f in facts:
        found.add(_Rec(item_id=f["units"][0]), f)
    graph = Graph()
    assemble(graph, found, units)
    assert not found.errors, found.errors
    return graph


def test_a_node_two_chats_state_shows_to_a_member_of_either_with_the_props_of_that_chat():
    units = {
        "u5": Label(1, 0, "U", frozenset({"demo"}), frozenset({C5})),
        "u6": Label(1, 0, "U", frozenset({"demo"}), frozenset({C6})),
    }
    graph = assembled(units, [_node("code:K", "u5", {"said": "five"}), _node("code:K", "u6", {"said": "six"})])
    node = graph.nodes["code:K"]
    assert node["props"] == {"said": "five"}  # the first branch's, never blank before the ontology's checks
    assert [label.audience for label, _ in node["guards"]] == [{C5}, {C6}]
    snap = Snapshot.from_graph(graph)
    for reader, said in ((frozenset({C5}), "five"), (frozenset({C6}), "six"), (frozenset({C5, C6}), "five")):
        read = snap.view(reader, (1, 0)).node("code:K")
        assert read["props"] == {"said": said} and "guards" not in read
    assert snap.view(frozenset(), (3, 0)).node("code:K") is None
    # The full-text row of the node is read by a member of either chat, and by no one else.
    (row,) = [r for r in search_rows(graph)[0] if r[0] == "code:K"]
    assert within(frozenset({C6}), row[-1]) and not within(frozenset({"chat:7"}), row[-1])


def test_a_node_of_two_dimensions_is_hidden_below_both_and_keeps_the_props_of_each():
    units = {"secret": Label(1, 0, "T", frozenset({"demo"})), "far": Label(0, 1, "T", frozenset({"demo"}))}
    graph = assembled(
        units,
        [
            _node("code:K", "secret", {"key": "k", "said": "secret"}),
            _node("code:K", "far", {"key": "k", "said": "far"}),
        ],
    )
    node = graph.nodes["code:K"]
    assert (node["label"].level, node["label"].location) == (0, 1)  # the first atom, lowest level first
    assert node["props"]["key"] == "k"
    view = Snapshot.from_graph(graph).view
    low = view(frozenset(), (0, 0)).node("code:K")["label"]  # a view reads any level; its caller keeps its ceiling
    assert not (low[0] <= 0 and low[1] <= 0)
    assert view(frozenset(), (1, 0)).node("code:K")["props"]["said"] == "secret"
    assert view(frozenset(), (0, 1)).node("code:K")["props"]["said"] == "far"


def test_an_edge_needs_its_ends_and_reads_through_either_chat_of_a_shared_end():
    units = {
        "u5": Label(1, 0, "U", frozenset({"demo"}), frozenset({C5})),
        "u6": Label(1, 0, "U", frozenset({"demo"}), frozenset({C6})),
    }
    edge = {
        "t": "edge",
        "src": "chat:m5",
        "rel": "mentions",
        "dst": "code:K",
        "props": {},
        "status": "parsed",
        "conf": 1.0,
        "units": ["u5"],
        "needs": [],
    }
    graph = assembled(
        units,
        [_node("code:K", "u5", {}), _node("code:K", "u6", {}), _node("chat:m5", "u5", {}, "Message"), edge],
    )
    (e,) = graph.edges.values()
    assert e["label"].audience == {C5} and "guards" not in e  # the chat 5 message and the chat 5 branch of K
    view = Snapshot.from_graph(graph).view
    assert [x["dst"] for x in view(frozenset({C5}), (1, 0)).edges_of("chat:m5")] == ["code:K"]
    assert view(frozenset({C6}), (1, 0)).edges_of("code:K") == []


def test_the_rows_of_an_element_with_guards_read_back_as_it_and_those_of_one_atom_are_unchanged():
    units = {
        "u5": Label(1, 0, "U", frozenset({"demo"}), frozenset({C5})),
        "u6": Label(1, 0, "U", frozenset({"demo"}), frozenset({C6})),
    }
    graph = assembled(units, [_node("code:K", "u5", {"a": 1}), _node("code:K", "u6", {"b": 2}), _node("x", "u5", {})])
    rows = graph_tables(graph)["nodes"]
    assert "label_guards" in rows["code:K"] and "label_guards" not in rows["x"]
    read = node_dict(node_row("code:K", graph.nodes["code:K"]))
    kept = graph.nodes["code:K"]
    assert node_row("code:K", {**read, "label": kept["label"]}) == node_row("code:K", kept)
    assert read_as(read, frozenset({C6}), None)["props"] == {"b": 2}
    assert node_dict(node_row("x", graph.nodes["x"])).get("guards") is None
    edge = {"src": "x", "rel": "r", "dst": "code:K", "props": {}, "status": "parsed", "conf": 1.0}
    edge["label"] = units["u5"]
    assert edge_dict(edge_row("e:1", edge)).get("guards") is None
    # A guard that cannot be read is one no reader passes.
    broken = (*node_row("x", graph.nodes["x"])[:11], "{not json")
    assert read_as(node_dict(broken), frozenset({C5}), None) is None

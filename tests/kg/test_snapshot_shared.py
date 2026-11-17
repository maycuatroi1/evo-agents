"""A snapshot keeps one object for each value its nodes and edges repeat (``evo_agents.kg.snapshot.Shared``): each
row read is objects of its own, and a large graph's hundreds of thousands of edges held as many copies of a few ids,
relations and labels, gigabytes of the api's memory. Sharing changes no value, and no type: 1 == 1.0 == True."""

from __future__ import annotations

from evo_agents.kg.snapshot import Shared


def fresh(text: str) -> str:
    return "".join(list(text))  # an equal string, another object, as each row read is


def test_an_edge_names_the_node_ids_and_repeats_the_relations_and_labels_as_one_object_each():
    ids = ("lumen-api:a.py", "lumen-api:b.py")
    one = Shared(ids)
    first = one.edge(
        {"src": fresh(ids[0]), "rel": fresh("calls"), "dst": fresh(ids[1]), "status": None, "label": (0, 1, 0, None)}
    )
    second = one.edge(
        {
            "src": fresh(ids[1]),
            "rel": fresh("calls"),
            "dst": fresh(ids[0]),
            "status": fresh("parsed"),
            "label": (0, 1, 0, None),
        }
    )
    assert first["src"] is ids[0] and first["dst"] is ids[1] and second["src"] is ids[1]
    assert first["rel"] is second["rel"] and first["label"] is second["label"]
    assert first["status"] is None and second["status"] == "parsed"


def test_equal_values_of_other_types_are_not_shared():
    one = Shared(())
    labels = [(1, 0, 0, None), (1.0, 0, 0, None), (True, 0, 0, None), (1, 0, 0, None)]
    kept = [one.label(label) for label in labels]
    assert [type(k[0]) for k in kept] == [int, float, bool, int]
    assert kept[3] is kept[0] and kept[1] is not kept[0] and kept[2] is not kept[0]
    assert one.text(1) == 1 and type(one.text(True)) is bool and one.text(None) is None

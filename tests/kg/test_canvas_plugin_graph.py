"""The knowledge graph of a project that enables the core plugin canvas beside srs and tasks, as criterion a7 of plan
living-lean-canvas names it, built on this machine from what the plugins' sources answer (``evo_agents.core_plugins.
canvas.kg`` beside the sources of srs and tasks, made into a build's sources by ``evo_agents.hub.kg_sources``).

Checked here: a canvas gives a Document with a Section per cell and per claim, and each claim a Claim node with the id
a mention of its code gets, holding its code alone; the use case a claim links implements it, the signal keys it links
and its test card's metric measure it (SignalKey nodes); kg_path goes from a claim to its use case, the task that
implements the use case and the plan step that implements the task, and to a signal key; a mention of a claim's code
in a document links to the claim; the canvas carries its label, and a sink below it sees neither the claim's section
nor its links, while a public document that names the claim shows the claim's code alone; without srs, a claim links
no use case in the graph. The kinds Claim and SignalKey and the relation measured_by are the hub's, added only. The
product of the canvas, Northwind Pantry, is made up. Nothing touches ~/.evo/kg or a hub.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

pytest.importorskip("sqlalchemy", reason="the kg source of canvas needs the hub-server extra")

from evo_agents.core_plugins.canvas import kg as canvas_kg
from evo_agents.core_plugins.canvas.store import CanvasRow, ClaimRow
from evo_agents.hub.kg_sources import checked, keep_sources, knowledge_with
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.protocol import finalize_item
from evo_agents.kg.replace import replace_source
from evo_agents.kg.schema import HUB_KINDS, HUB_RELATIONS, known_kinds, validate_ontology
from evo_agents.kg.serve import Session, ToolError
from evo_agents.kg.store import Store
from evo_agents.kg.text import split_markdown
from tests.kg.test_srs_tasks_plugin_graph import (
    AGENT,
    KNOWLEDGE,
    LABEL,
    NARROW,
    PROJECT,
    STEP,
    TICKET,
    USE_CASE,
    WHEN,
    edge_between,
    machine_items,
    srs_source,
    tasks_source,
)

SLUG = "northwind-pantry"
CANVAS = f"plugin.canvas:canvas:{SLUG}"
CLAIM = "claim:CL-1"
LEAD_DAYS = "pantry.reorder.lead_days"
STOCKOUTS = "pantry.stock.outs_per_week"
PUBLIC_NOTE = "# Pantry notes\n\nWe test CL-1 first, as the brainstorm said.\n"


def canvas_row(**values) -> CanvasRow:
    row = {
        "id": 1,
        "slug": SLUG,
        "name": "Northwind Pantry",
        "description": "Stock reordering for corner shops",
        "label": dict(LABEL),
        "created_by": "alice",
        "created_at": WHEN,
        "updated_at": WHEN,
        "archived_at": None,
    }
    return CanvasRow(**{**row, **values})


def claim_row(number: int, cell: str, text: str, **values) -> ClaimRow:
    row = {
        "id": number,
        "canvas_id": 1,
        "number": number,
        "cell": cell,
        "position": number,
        "text": text,
        "status": "assumption",
        "confidence": "medium",
        "test_card": None,
        "rev": 1,
        "label": dict(LABEL),
        "created_by": "alice",
        "created_at": WHEN,
        "updated_at": WHEN,
        "archived_at": None,
    }
    return ClaimRow(**{**row, **values})


CARD = {"hypothesis": "Owners reorder sooner", "test": "Watch lead days", "metric": STOCKOUTS, "deadline": "2026-12-01"}
CLAIMS = [
    claim_row(
        1,
        "solution",
        "Ordering from the shelf with one tap cuts stock-outs",
        status="testing",
        test_card=CARD,
        use_cases=["UC ORD-1"],
        signal_keys=[LEAD_DAYS],
        updated_at=WHEN + timedelta(hours=1),
    ),
    claim_row(2, "problem", "Corner shops run out of stock between deliveries, see CL-1", use_cases=["UC NONE-9"]),
]


def canvas_source(with_srs: bool = True, claims: list[ClaimRow] | None = None):
    item = canvas_kg.canvas_item("plugin.canvas", canvas_row(), CLAIMS if claims is None else claims, with_srs)
    answer = {"items": [item], "identifiers": canvas_kg.identifiers()}
    return checked("plugin.canvas", "canvas", answer, KNOWLEDGE, PROJECT)


@pytest.fixture
def shop(tmp_path):
    """The project shop of ``tests.kg.test_srs_tasks_plugin_graph``, with a public note that names CL-1, and a
    function that builds it with the plugins' sources it is given."""
    root = tmp_path / "shop-harness"
    root.mkdir()
    (root / "harness.yaml").write_text("name: shop\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8")
    home = tmp_path / "kg-home"

    def build(sources: list, bindings: bool = True):
        text = yaml.safe_dump(knowledge_with(KNOWLEDGE, sources), allow_unicode=True, sort_keys=True)
        (root / "knowledge.yaml").write_text(text, encoding="utf-8")
        project = load_project_at(root, home)
        corpus = project.corpus()
        items = machine_items()
        note = {"format": "markdown", "text": PUBLIC_NOTE}
        found = {
            "id": "harness:file:docs/pantry.md",
            "kind": "markdown",
            "rev": "1",
            "rev_time": "2026-10-01T00:00:00Z",
        }
        found |= {"body": note, "props": {"path": "docs/pantry.md"}, "fragments": split_markdown(PUBLIC_NOTE)}
        items["harness"].append(finalize_item(found))
        if not bindings:  # a binding to a use case fails the build of a project without srs
            items["harness"] = [held for held in items["harness"] if "bindings" not in held["id"]]
        try:
            for source, held in items.items():
                replace_source(corpus, source, held, connector="test", project=PROJECT)
        finally:
            corpus.close()
        keep_sources(project, sources)
        report = build_project(project)
        assert report.ok, report.errors
        return project, Store.for_project(project), report.build_id

    return build


def test_canvas_claims_are_declared_nodes_with_their_use_case_and_signal_keys(shop):
    project, store, b = shop([srs_source(), tasks_source(), canvas_source()])
    assert store.node(CANVAS, b)["kind"] == "Document"
    claim = store.node(CLAIM, b)
    assert claim["kind"] == "Claim" and claim["status"] == "declared"
    assert (claim["name"], claim["props"]) == ("CL-1", {"code": "CL-1"})  # its text and status stay in its section
    section = store.node(f"{CANVAS}#CL-1", b)
    assert (section["kind"], section["name"]) == ("Section", "Claim 1 in Solution")
    assert edge_between(store, b, f"{CANVAS}#CL-1", "part_of", f"{CANVAS}#solution") is not None
    assert edge_between(store, b, f"{CANVAS}#CL-1", "defines", CLAIM)["status"] == "declared"
    assert edge_between(store, b, USE_CASE, "implements", CLAIM)["status"] == "declared"
    for key in (LEAD_DAYS, STOCKOUTS):  # the key it links, and the one its test card measures
        node = store.node(f"signalkey:{key}", b)
        assert (node["kind"], node["name"], node["props"]) == ("SignalKey", key, {"key": key})
        assert edge_between(store, b, CLAIM, "measured_by", f"signalkey:{key}")["status"] == "declared"
    # CL-2 links a use case no approved version holds: the edge dangles and is dropped
    assert store.node("usecase:UC NONE-9", b) is None
    assert store.node("claim:CL-2", b)["kind"] == "Claim"
    found = Session(project, AGENT).call("kg_search", {"query": "one tap stock-outs"})["structuredContent"]
    assert f"{CANVAS}#CL-1" in [hit["id"] for hit in found["results"]]  # search finds a claim by what it says


def test_canvas_kg_path_goes_from_a_claim_to_its_use_case_task_and_step_and_to_its_signal_key(shop):
    project, _, _ = shop([srs_source(), tasks_source(), canvas_source()])
    session = Session(project, AGENT)
    chains = session.call("kg_path", {"from": CLAIM})["structuredContent"]
    assert {USE_CASE, TICKET, STEP} <= {found["id"] for found in chains["nodes"]}
    path = session.call("kg_path", {"from": CLAIM, "to": STEP})["structuredContent"]
    assert path["found"] and [found["id"] for found in path["nodes"]] == [CLAIM, USE_CASE, TICKET, STEP]
    assert [edge["rel"] for edge in path["edges"]] == ["implements", "implements", "implements"]
    measured = session.call("kg_path", {"from": CLAIM, "to": f"signalkey:{LEAD_DAYS}"})["structuredContent"]
    assert measured["found"] and measured["hops"] == 1


def test_canvas_mentions_of_a_claim_code_link_to_the_claim_and_show_its_code_alone_below_its_label(shop):
    project, store, b = shop([srs_source(), tasks_source(), canvas_source()])
    assert edge_between(store, b, "harness:file:docs/pantry.md#pantry-notes", "mentions", CLAIM) is not None
    assert edge_between(store, b, f"{CANVAS}#CL-2", "mentions", CLAIM) is not None  # one claim names another
    assert Session(project, AGENT).call("kg_node", {"id": f"{CANVAS}#CL-1"})["structuredContent"]["node"]
    # A public note names CL-1: the claim is public with its code alone; its section and links stay internal.
    _, shown = Session(project, NARROW).tool_kg_node(CLAIM)
    assert shown["node"]["label"]["level"] == "public" and shown["node"]["props"] == {"code": "CL-1"}
    for hidden in (f"{CANVAS}#CL-1", CANVAS, f"signalkey:{LEAD_DAYS}"):
        with pytest.raises(ToolError):
            Session(project, NARROW).tool_kg_node(hidden)
    narrow = Session(project, NARROW).call("kg_path", {"from": CLAIM})["structuredContent"]
    assert [found["id"] for found in narrow["nodes"]] == [CLAIM]  # its links carry the canvas's label


def test_canvas_without_srs_links_no_use_case_and_an_archived_claim_is_left_out(shop):
    project, store, b = shop([tasks_source(), canvas_source(with_srs=False, claims=CLAIMS[:1])], bindings=False)
    assert store.node(CLAIM, b)["kind"] == "Claim" and store.node("claim:CL-2", b) is None
    assert not [edge for edge in store.edges_of(CLAIM, b, limit=50) if edge["rel"] == "implements"]
    assert edge_between(store, b, CLAIM, "measured_by", f"signalkey:{LEAD_DAYS}") is not None


def test_canvas_item_carries_the_canvas_label_page_and_latest_change():
    item = canvas_kg.canvas_item("plugin.canvas", canvas_row(), CLAIMS, True, ("https://hub.example.org/", "shop one"))
    assert item["uri"] == "https://hub.example.org/p/shop%20one/x/canvas/canvases/northwind-pantry"
    assert item["rev_time"] == "2026-10-19T04:00:00.000000Z" and item["label"] == LABEL
    anchors = [fragment["anchor"] for fragment in item["fragments"]]
    assert anchors == ["_preamble", "problem", "CL-2", "solution", "CL-1"]  # cells in the canvas's order
    text = next(fragment["text"] for fragment in item["fragments"] if fragment["anchor"] == "CL-1")
    assert text.startswith("Ordering from the shelf") and "Status: testing." in text
    assert f"Signal keys: {LEAD_DAYS}." in text and "UC ORD-1" not in text  # a use case is an edge only
    assert canvas_kg.measured_by(CLAIMS[0]) == [LEAD_DAYS, STOCKOUTS]
    assert canvas_kg.measured_by(claim_row(3, "problem", "x", test_card={"metric": "feedback"})) == []
    (found,) = canvas_source().items
    assert found["label"]["level"] == "internal"


def test_canvas_kinds_and_relation_are_the_hubs_added_only():
    assert {"Claim", "SignalKey"} <= set(HUB_KINDS) and {"UseCase", "Ticket"} <= set(HUB_KINDS)
    assert HUB_RELATIONS["measured_by"] == ("Claim", "SignalKey")
    assert HUB_RELATIONS["implements"] == ("*", "*")  # a use case implements a claim with the relation tasks use
    assert {"Claim", "SignalKey"} <= known_kinds({"node_kinds": ["Shelf"]})
    issues = validate_ontology({"node_kinds": ["Claim"], "relations": ["measured_by"]})
    assert [issue.path for issue in issues] == ["node_kinds[0]", "relations[0]"]

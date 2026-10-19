"""The knowledge graph of a project that enables the core plugins srs and tasks, as criterion a4 of plan
srs-tasks-plugins names it, built on this machine from what the plugins' sources answer (``evo_agents.core_plugins.
srs.kg``, ``evo_agents.core_plugins.tasks.kg``, made into a build's sources by ``evo_agents.hub.kg_sources``).

Checked here: the approved version of a document gives a Document with a Section per heading, UseCase and Requirement
nodes with the ids a mention of their codes gets, declared ``defines``, ``child_of`` and ``mentions`` edges, and the
template's code patterns as identifiers, so a document that names a code and a binding from a test reach the very
node; a task gives a Ticket that implements its use case and that its plan step implements; kg_path goes from the use
case to the task, the step, the test and the commit the step's evidence names; the plugins' data carries its rows'
labels and a sink whose clearance is below them does not see it, while a use case that a public document names is
public with its code alone; a build of unchanged data writes nothing and builds nothing, and ``--verify`` gives the
same hash; a change or a removal is written once, and a plugin turned off leaves nothing. Also how a source's answer
is read: labels, ids, identifiers. Nothing touches ~/.evo/kg or a hub.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evo_agents.core_plugins.srs import kg as srs_kg
from evo_agents.core_plugins.srs import markdown
from evo_agents.core_plugins.srs.store import DocumentRow, Snapshot, VersionRow
from evo_agents.core_plugins.srs.template import template_of
from evo_agents.core_plugins.tasks import DEFAULT_STATUSES
from evo_agents.core_plugins.tasks import kg as tasks_kg
from evo_agents.core_plugins.tasks.settings import Settings, Status
from evo_agents.core_plugins.tasks.store import People, TaskRow
from evo_agents.hub import kg_sources
from evo_agents.hub.kg_sources import SourceFailure, checked, keep_sources, knowledge_with
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.protocol import finalize_item
from evo_agents.kg.replace import replace_source
from evo_agents.kg.serve import Session, ToolError
from evo_agents.kg.store import Store
from evo_agents.kg.text import split_markdown

PROJECT = "shop"
SAMPLE_SRS = Path(__file__).parent.parent / "hub" / "fixtures" / "srs" / "demo-shop-srs.md"
REFUNDS = """# 2. Use Case Specifications

## 2.1. Refunds

#### UC PAY-1: Refund a payment

The finance team refunds a payment.
"""
AGENT = "agent"
NARROW = "narrow"  # clears public only
SHA = "abc1234def5678abc1234def5678abc1234def56"
KNOWLEDGE = {
    "version": 1,
    "project": PROJECT,
    "policy": {
        "levels": ["public", "internal", "customer", "secret"],
        "sinks": [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": NARROW, "kind": "agent-session", "clearance": {"level": "public"}},
        ],
    },
    "sources": [
        {"id": "harness", "connector": "harness", "label": {"level": "public", "integrity": "U"}},
        {"id": "code", "connector": "git", "label": {"level": "internal", "integrity": "U"}},
    ],
}
PLAN = {
    "id": "rollout",
    "title": "Ship ordering",
    "goal": "Customers order books.",
    "steps": [
        {
            "id": 1,
            "title": "Place orders",
            "repo": "shop",
            "status": "done",
            "evidence": f"shop@{SHA}: ordering ships",
        }
    ],
}
BINDINGS = """version: 1
bindings:
  - from: shop:tests/test_orders.py::test_place_order
    rel: verifies
    to: "usecase:UC ORD-1"
"""
TEST_CODE = "def test_place_order():\n    assert True\n"
GUIDE = "# Orders\n\nThe checkout follows UC ORD-1 and its cart rules, BR ORD1-1.\n"
WHEN = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
LABEL = {"level": "internal", "integrity": "U", "projects": [PROJECT]}
USE_CASE = "usecase:UC ORD-1"
TICKET = "plugin.tasks:ticket:T-1"
STEP = "plan:rollout/step:1"


def item(item_id: str, kind: str, text: str, fmt: str, **props) -> dict:
    """A finished item of a machine's source, as a connector gives it."""
    body = {"format": fmt, "text": text, **({"language": "python"} if fmt == "code" else {})}
    return finalize_item(
        {"id": item_id, "kind": kind, "rev": "1", "rev_time": "2026-10-01T00:00:00Z", "body": body, "props": props}
    )


def machine_items() -> dict[str, list[dict]]:
    plan = item("harness:file:plans/active/rollout.yaml", "plan", yaml.safe_dump(PLAN), "yaml")
    plan["props"].update(path="plans/active/rollout.yaml", repo="harness")
    binding = item("harness:file:bindings/orders.yaml", "binding", BINDINGS, "yaml", path="bindings/orders.yaml")
    test = item("code:file:tests/test_orders.py", "code", TEST_CODE, "code", path="tests/test_orders.py", repo="shop")
    guide = item("harness:file:docs/orders.md", "markdown", GUIDE, "markdown", path="docs/orders.md")
    guide["fragments"] = split_markdown(GUIDE)
    return {"harness": [finalize_item(plan), binding, finalize_item(guide)], "code": [test]}


def snapshot(text: str, slug: str, keys: str, screens: bool = False) -> tuple[DocumentRow, Snapshot]:
    """A document whose version 2 was approved, with ``text`` as its content and its items keyed ``<keys><n>``."""
    doc = markdown.parse(text, template_of())
    for index, found in enumerate(doc.items):
        found.key = f"{keys}{index:02d}"
    if screens:  # UC ORD-1 uses the screen Cart, as a save's links set it
        cart = next(found for found in doc.items if found.kind == "screen" and found.title == "Cart")
        next(found for found in doc.items if found.code == "UC ORD-1").refs = [("uses_screen", cart.key)]
    version = VersionRow(
        id=1,
        number=2,
        parent_number=1,
        state="approved",
        round=1,
        author="alice",
        author_id=1,
        drafted_by_run=None,
        summary="",
        content_sha256="0" * 64,
        preamble_sha256=None,
        created_at=WHEN,
        submitted_at=WHEN,
        decided_at=WHEN,
    )
    row = DocumentRow(
        id=1,
        slug=slug,
        title=slug.replace("-", " ").title(),
        kind="srs",
        template="srs-basic",
        approved_version=2,
        label=dict(LABEL),
        created_by="alice",
        created_at=WHEN,
        archived_at=None,
        latest_version=2,
        latest_state="approved",
    )
    return row, Snapshot(version, doc, {})


def srs_source(secret: bool = True):
    row, snap = snapshot(SAMPLE_SRS.read_text(encoding="utf-8"), "demo-shop", "k", screens=True)
    items = [srs_kg.document_item("plugin.srs", row, snap)]
    if secret:
        refunds, snap = snapshot(REFUNDS, "refunds", "r")
        items.append(srs_kg.document_item("plugin.srs", replace(refunds, label={**LABEL, "level": "secret"}), snap))
    answer = {"items": items, "identifiers": srs_kg.identifiers(template_of())}
    return checked("plugin.srs", "srs", answer, KNOWLEDGE, PROJECT)


CONFIG = Settings("T", tuple(Status(**status) for status in DEFAULT_STATUSES), ("task", "bug", "story"))


def task(number: int, title: str, status: str = "in_progress", label: dict | None = None) -> TaskRow:
    return TaskRow(
        id=number,
        number=number,
        type="story",
        title=title,
        description_md="Hold the stock as BR ORD1-2 says.",
        status=status,
        priority="high",
        due_on=date(2026, 11, 1),
        sprint="Sprint 1",
        parent_number=None,
        label=label or dict(LABEL),
        external_ref=None,
        created_by="alice",
        created_at=WHEN,
        updated_at=WHEN,
    )


def link(kind: str, **values) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, **{"plan_id": None, "step_id": None, "srs_kind": None, "code": None, **values})


def tasks_source(status: str = "in_progress", second: bool = True):
    links = [link("srs_item", srs_kind="use_case", code="UC ORD-1"), link("plan_step", plan_id="rollout", step_id="1")]
    items = [tasks_kg.ticket_item("plugin.tasks", CONFIG, task(1, "Checkout", status), People(["alice"]), links)]
    if second:
        items.append(tasks_kg.ticket_item("plugin.tasks", CONFIG, task(2, "Gift cards"), None, []))
    elsewhere = {**LABEL, "projects": [PROJECT, "other"]}  # a row of two projects stays out of the graph
    items.append(tasks_kg.ticket_item("plugin.tasks", CONFIG, task(3, "Shared", label=elsewhere), None, []))
    return checked("plugin.tasks", "tasks", {"items": items, "identifiers": []}, KNOWLEDGE, PROJECT)


@pytest.fixture
def shop(tmp_path):
    """The project shop: a harness, its machine's items in the corpus, and a function that builds it with the plugins'
    sources it is given."""
    root = tmp_path / "shop-harness"
    root.mkdir()
    (root / "harness.yaml").write_text("name: shop\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8")
    home = tmp_path / "kg-home"

    def build(sources: list, **options):
        text = yaml.safe_dump(knowledge_with(KNOWLEDGE, sources), allow_unicode=True, sort_keys=True)
        (root / "knowledge.yaml").write_text(text, encoding="utf-8")
        project = load_project_at(root, home)
        corpus = project.corpus()
        try:
            for source, items in machine_items().items():
                replace_source(corpus, source, items, connector="test", project=PROJECT)
        finally:
            corpus.close()
        keep_sources(project, sources)
        report = build_project(project, **options)
        assert report.ok, report.errors
        return project, report

    return build


def graph(project, report):
    store = Store.for_project(project)
    return store, report.build_id


def edge_between(store, b: int, src: str, rel: str, dst: str) -> dict | None:
    return next(
        (e for e in store.edges_of(src, b, limit=500) if (e["src"], e["rel"], e["dst"]) == (src, rel, dst)), None
    )


def test_srs_use_cases_and_requirements_are_declared_nodes_of_the_approved_version(shop):
    project, report = shop([srs_source(), tasks_source()])
    store, b = graph(project, report)
    doc = "plugin.srs:document:demo-shop"
    use_case = store.node(USE_CASE, b)
    assert use_case["kind"] == "UseCase" and use_case["status"] == "declared"
    assert (use_case["name"], use_case["props"]) == (
        "UC ORD-1",
        {"code": "UC ORD-1"},
    )  # its content stays in its section
    assert store.node(doc, b)["kind"] == "Document"
    section = f"{doc}#k08"  # the heading of UC ORD-1, by its key
    assert store.node(section, b)["kind"] == "Section"
    assert edge_between(store, b, section, "defines", USE_CASE)["status"] == "declared"
    rule = store.node("requirement:BR ORD1-1", b)
    assert rule["kind"] == "Requirement" and rule["status"] == "declared"
    assert edge_between(store, b, "requirement:BR ORD1-1", "child_of", USE_CASE)["status"] == "declared"
    for code in ("NFR-SEC-01", "CBR 2"):  # table rows: defined by the section of the heading above them
        assert store.node(f"requirement:{code}", b)["status"] == "declared"
    assert edge_between(store, b, f"{doc}#k23", "defines", "requirement:NFR-SEC-01") is not None
    assert edge_between(store, b, USE_CASE, "mentions", f"{doc}#k18")["status"] == "declared"  # the screen Cart
    found = Session(project, AGENT).call("kg_search", {"query": "UC-ORD-001"})["structuredContent"]
    assert section in [hit["id"] for hit in found["results"]]  # an alias finds the section that defines the use case


def test_srs_codes_link_mentions_and_bindings_to_the_declared_node(shop):
    project, report = shop([srs_source(), tasks_source()])
    store, b = graph(project, report)
    assert edge_between(store, b, "harness:file:docs/orders.md#orders", "mentions", USE_CASE) is not None
    symbol = "symbol:code:tests/test_orders.py::test_place_order"
    assert edge_between(store, b, symbol, "verifies", USE_CASE)["status"] == "declared"
    assert edge_between(store, b, TICKET, "mentions", "requirement:BR ORD1-2") is not None  # the task's description


def test_tasks_plugin_tickets_implement_use_cases_and_steps_implement_tickets(shop):
    project, report = shop([srs_source(), tasks_source()])
    store, b = graph(project, report)
    ticket = store.node(TICKET, b)
    assert ticket["kind"] == "Ticket" and ticket["name"] == "T-1 Checkout"
    assert {key: ticket["props"][key] for key in ("number", "status", "status_group", "sprint", "assignees")} == {
        "number": "T-1",
        "status": "in_progress",
        "status_group": "doing",
        "sprint": "Sprint 1",
        "assignees": ["alice"],
    }
    assert edge_between(store, b, TICKET, "implements", USE_CASE)["status"] == "declared"
    assert edge_between(store, b, STEP, "implements", TICKET)["status"] == "declared"
    assert store.node("plugin.tasks:ticket:T-3", b) is None  # its label names another project


def test_srs_kg_path_goes_from_a_use_case_to_its_task_step_test_and_commit(shop):
    project, report = shop([srs_source(), tasks_source()])
    session = Session(project, AGENT)
    chains = session.call("kg_path", {"from": USE_CASE})["structuredContent"]
    reached = {found["id"] for found in chains["nodes"]}
    assert {TICKET, STEP, "symbol:code:tests/test_orders.py::test_place_order"} <= reached
    commit = f"commit:shop@{SHA}"  # the Commit the evidence names, as no git source read it
    path = session.call("kg_path", {"from": USE_CASE, "to": commit})["structuredContent"]
    assert path["found"] and [found["id"] for found in path["nodes"]] == [USE_CASE, TICKET, STEP, commit]


def test_srs_plugin_data_carries_its_rows_label_and_a_sink_below_it_sees_nothing(shop):
    project, report = shop([srs_source(), tasks_source()])
    store, b = graph(project, report)
    doc = "plugin.srs:document:demo-shop"
    for internal in ("usecase:UC ORD-2", "requirement:NFR-SEC-01", doc, f"{doc}#k08", TICKET):
        assert store.node(internal, b)["label"][0] == 1, internal
    assert store.node("usecase:UC PAY-1", b)["label"][0] == 3  # the refunds document is secret
    agent = Session(project, AGENT)
    assert agent.call("kg_node", {"id": TICKET})["structuredContent"]["node"]["label"]["level"] == "internal"
    hidden = [(AGENT, "usecase:UC PAY-1"), (NARROW, "usecase:UC ORD-2"), (NARROW, TICKET), (NARROW, f"{doc}#k08")]
    for sink, node_id in hidden:
        with pytest.raises(ToolError):
            Session(project, sink).tool_kg_node(node_id)
    # A public document names UC ORD-1: the use case is public with its code alone, its section and task are not.
    _, shown = Session(project, NARROW).tool_kg_node(USE_CASE)
    assert shown["node"]["label"]["level"] == "public" and shown["node"]["props"] == {"code": "UC ORD-1"}


def test_srs_tasks_plugin_unchanged_data_builds_nothing_and_verifies(shop):
    sources = [srs_source(), tasks_source()]
    project, first = shop(sources)
    runs = project.corpus().runs(limit=100)
    project, again = shop([srs_source(), tasks_source()])  # read again: the same answers
    assert again.skipped and again.content_hash == first.content_hash
    assert project.corpus().runs(limit=100) == runs  # nothing written: no run, no log
    project, forced = shop(sources, force=True, verify=True)
    assert forced.content_hash == first.content_hash and forced.verify["match"]


def test_srs_tasks_plugin_a_change_or_a_removal_is_written_once(shop):
    project, first = shop([srs_source(), tasks_source()])
    project, moved = shop([srs_source(), tasks_source(status="done", second=False)])
    assert not moved.skipped and moved.content_hash != first.content_hash
    store, b = graph(project, moved)
    assert store.node(TICKET, b)["props"]["status"] == "done"
    assert store.node("plugin.tasks:ticket:T-2", b) is None  # removed: a tombstone
    (latest,) = project.corpus().runs("plugin.tasks", limit=1)
    assert latest["detail"] == {"written": 1, "removed": 1}
    project, dropped = shop([srs_source()])  # the project turned tasks off
    store, b = graph(project, dropped)
    assert store.node(TICKET, b) is None and store.node(USE_CASE, b) is not None
    assert "plugin.tasks" not in project.corpus().sources()


def test_srs_source_answers_are_read_with_their_labels_ids_and_identifiers():
    doc = {"id": "plugin.srs:document:x", "kind": "doc", "title": "X", "label": {"level": "top-secret"}}
    (found,) = checked("plugin.srs", "srs", {"items": [doc]}, KNOWLEDGE, PROJECT).items
    assert found["label"] == {"level": "secret", "integrity": "U"}  # a level the ladder lacks reads as its highest
    assert found["rev"] == found["hash"] and found["rev_time"] == "1970-01-01T00:00:00Z"
    placed = {**doc, "label": {"level": "public", "location": "eu", "integrity": "T"}}
    assert checked("plugin.srs", "srs", {"items": [placed]}, KNOWLEDGE, PROJECT).items[0]["label"] == {
        "level": "public",
        "location": "any",  # the ladder's highest location
        "integrity": "T",
    }
    unlabelled = checked("plugin.srs", "srs", {"items": [{**doc, "label": None}]}, KNOWLEDGE, PROJECT)
    assert unlabelled.items == () and unlabelled.left_out == 1
    refused = [
        {"items": [{**doc, "id": "plugin.tasks:ticket:T-1"}]},  # another source's id
        {"items": [{**doc, "secret": 1}]},  # a key kg/1 does not allow
        {"items": [doc, doc]},  # one id twice
        {"items": [], "identifiers": [{"kind": "UseCase", "pattern": "("}]},
        {"items": [], "identifiers": [{"kind": "UseCase", "pattern": "x*"}]},  # matches nothing at all
        {"items": [], "identifiers": [{"kind": "usecase", "pattern": "UC"}]},
        [],
    ]
    for answer in refused:
        with pytest.raises(SourceFailure):
            checked("plugin.srs", "srs", answer, KNOWLEDGE, PROJECT)


def test_srs_document_links_its_page_on_the_hub_when_the_hub_knows_its_url():
    row, snap = snapshot(SAMPLE_SRS.read_text(encoding="utf-8"), "demo-shop", "k", screens=True)
    assert "uri" not in srs_kg.document_item("plugin.srs", row, snap)  # a hub without EVO_HUB_PUBLIC_URL
    item = srs_kg.document_item("plugin.srs", row, snap, ("https://hub.example.org/", "shop one"))
    assert item["uri"] == "https://hub.example.org/p/shop%20one/x/srs/documents/demo-shop"
    (found,) = checked("plugin.srs", "srs", {"items": [item]}, KNOWLEDGE, PROJECT).items
    assert found["uri"] == item["uri"] and found["label"]["level"] == LABEL["level"]


def test_srs_identifiers_find_codes_inside_text_and_come_before_the_projects_own():
    patterns = {entry["kind"]: entry["pattern"] for entry in srs_kg.identifiers(template_of())}
    assert set(patterns) == {"UseCase", "Requirement"}
    use_case = srs_kg.identifiers(template_of())[0]["pattern"]
    assert re.findall(use_case, "see UC ORD-1, not UC ORD-12x or XUC ORD-1") == ["UC ORD-1"]
    source = kg_sources.PluginSource("plugin.srs", "srs", (), ({"kind": "UseCase", "pattern": use_case},))
    own = {
        **KNOWLEDGE,
        "identifiers": [{"kind": "Requirement", "pattern": "P[0-9]"}, {"kind": "UseCase", "pattern": use_case}],
    }
    shadow = {"id": "plugin.srs", "connector": "git"}  # a source of the config under a plugin's id gives way
    merged = knowledge_with({**own, "sources": [*KNOWLEDGE["sources"], shadow]}, [source])
    assert [entry["pattern"] for entry in merged["identifiers"]] == [use_case, "P[0-9]"]
    assert merged["sources"][-1] == {
        "id": "plugin.srs",
        "connector": "plugin",
        "label": {"level": "public", "location": "any", "integrity": "T"},
    }
    assert [entry["id"] for entry in merged["sources"]] == ["harness", "code", "plugin.srs"]
    assert knowledge_with(KNOWLEDGE, []) is KNOWLEDGE  # no plugin source: the config as it is

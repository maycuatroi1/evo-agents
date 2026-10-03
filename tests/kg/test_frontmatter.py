"""A frontmatter ``id`` that is a code makes its document the place that code is defined."""

import pytest

from evo_agents.kg.build import build_project, multiple_definitions
from evo_agents.kg.pipeline.stages import frontmatter, structure_item
from evo_agents.kg.project import load_project_at
from evo_agents.kg.status import project_status, render_status
from evo_agents.kg.store import Graph, Store
from evo_agents.kg.sync import sync_project
from evo_agents.kg.text import split_markdown
from tests.kg.projects import make_project, write

USECASE = [{"kind": "UseCase", "pattern": "\\bKB-\\d{2}\\b"}]
SCREEN = "  - kind: Screen\n    pattern: '\\bSCR-\\d+\\b'\n"


def markdown_record(text: str) -> dict:
    frags = [{k: v for k, v in f.items() if k != "text"} for f in split_markdown(text)]
    return {"id": "docs:file:a.md", "kind": "markdown", "fragments": frags}


def structure(text: str, identifiers=USECASE) -> list[dict]:
    return structure_item(markdown_record(text), "docs", text, "auto", identifiers)


def sync_build(project, **kw):
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    report = build_project(project, **kw)
    assert report.ok, report.errors
    return report


def live_edges(store: Store, b: int) -> dict[tuple, str]:
    rows = store.db.execute(
        "SELECT src, rel, dst, status FROM edges WHERE tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)", {"b": b}
    )
    return {(src, rel, dst): status for src, rel, dst, status in rows}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nid: KB-01\n---\n# Title\n", ("id: KB-01\n", 3)),
        ("---\r\nid: KB-01\r\n---\r\nbody\r\n", ("id: KB-01\r\n", 3)),
        ("﻿---\nid: KB-01\ntitle: t\n...\n", ("id: KB-01\ntitle: t\n", 4)),
        ("---\n---\n", ("", 2)),
        ("---\nid: KB-01\n", None),  # never closed
        ("\n---\nid: KB-01\n---\n", None),  # not at the start
        ("# Title\n\n---\nid: KB-01\n---\n", None),
    ],
)
def test_frontmatter_block_and_closing_line(text, expected):
    assert frontmatter(text) == expected


def test_frontmatter_id_defines_its_code_from_the_preamble():
    facts = structure("---\nid: KB-05\ntitle: Search\n---\n\n# Search\n\nText.\n")
    unit, where = "docs:file:a.md#_preamble", [["docs:file:a.md#_preamble", "lines 1-4"]]
    assert [(f["t"], f.get("id") or (f["src"], f["rel"], f["dst"])) for f in facts] == [
        ("node", "usecase:KB-05"),
        ("edge", ("docs:file:a.md", "defines", "usecase:KB-05")),
    ]
    code, defines = facts
    assert (code["kind"], code["name"], code["props"]) == ("UseCase", "KB-05", {"code": "KB-05"})
    assert all(f["status"] == "parsed" and f["units"] == [unit] and f["where"] == where for f in facts)


def test_frontmatter_split_by_a_comment_heading_falls_back_to_the_item_unit():
    facts = structure("---\nid: KB-05\n# a YAML comment, read as a heading\n---\n")
    assert {u for f in facts for u in f["units"]} == {"docs:file:a.md#"}


@pytest.mark.parametrize(
    "head",
    [
        "id: KB-123",  # the pattern must match the whole value
        "id: see KB-05",
        "id: 5",
        "title: KB-05",
        "id: KB-05\nderived_from: docs/original.md",  # a marked copy defines nothing
    ],
)
def test_frontmatter_without_a_code_id_defines_nothing(head):
    assert structure(f"---\n{head}\n---\n# T\n") == []


def test_frontmatter_is_not_read_without_identifiers():
    assert structure("---\nid: [unclosed\n---\n", identifiers=[]) == []


def test_frontmatter_that_is_not_yaml_is_an_issue():
    assert structure("---\nid: [unclosed\n---\n# T\n") == [
        {
            "t": "issue",
            "message": "docs:file:a.md: frontmatter is not valid YAML; its id is ignored",
            "unit": "docs:file:a.md#_preamble",
        }
    ]


def test_frontmatter_definitions_count_places_not_sections():
    graph = Graph()
    nodes = {
        "doc:a": ("Document", {}),
        "doc:a#s": ("Section", {"item": "doc:a"}),
        "doc:b": ("Document", {}),
        "usecase:KB-01": ("UseCase", {}),
        "usecase:KB-02": ("UseCase", {}),
        "symbol:x": ("Symbol", {}),
    }
    graph.nodes = {nid: {"kind": kind, "props": props} for nid, (kind, props) in nodes.items()}
    edges = [
        ("doc:a", "usecase:KB-01"),
        ("doc:a#s", "usecase:KB-01"),  # its own section: still one place
        ("doc:a", "usecase:KB-02"),
        ("doc:b", "usecase:KB-02"),
        ("doc:a", "symbol:x"),
        ("doc:b", "symbol:x"),  # not a code kind
    ]
    graph.edges = {f"e{i}": {"src": s, "rel": "defines", "dst": d} for i, (s, d) in enumerate(edges)}
    assert multiple_definitions(graph, {"UseCase"}) == {
        "codes": 1,
        "samples": [{"code": "usecase:KB-02", "places": ["doc:a", "doc:b"]}],
    }


def test_frontmatter_status_lists_codes_defined_twice(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    root = project.harness.root
    write(root / "specs/search.md", "---\nid: KB-05\n---\n\n# Search\n\nRanks results.\n")
    write(root / "specs/search-copy.md", "---\nid: KB-05\nderived_from: specs/search.md\n---\n\n# Search, copy\n")
    write(root / "specs/export.md", "---\nid: KB-06\n---\n\n# Export\n")
    write(root / "specs/export-old.md", "---\nid: KB-06\n---\n\n# Export, old\n")
    write(root / "specs/broken.md", "---\nid: [KB-07\n---\n\n# Broken\n")
    report = sync_build(project, verify=True, cold=True)
    edges = live_edges(Store.for_project(project), report.build_id)

    def doc(name: str) -> str:
        return f"harness:file:specs/{name}.md"

    assert edges[(doc("search"), "defines", "usecase:KB-05")] == "parsed"
    assert (doc("search"), "mentions", "usecase:KB-05") not in edges  # defined, not also mentioned
    assert (doc("search-copy"), "defines", "usecase:KB-05") not in edges
    assert (doc("search-copy"), "mentions", "usecase:KB-05") in edges
    assert (doc("export"), "defines", "usecase:KB-06") in edges
    assert (doc("export-old"), "defines", "usecase:KB-06") in edges
    assert f"{doc('broken')}: frontmatter is not valid YAML; its id is ignored" in report.issues

    status = project_status(project)
    assert status["multiple_definitions"]["codes"] == 1
    assert status["multiple_definitions"]["samples"] == [
        {"code": "usecase:KB-06", "places": [doc("export-old"), doc("export")]}
    ]
    assert "derived_from" in status["multiple_definitions"]["hint"]
    text = render_status(status)
    assert "codes defined in two or more places: 1; keep one definition per code" in text
    assert f"        usecase:KB-06: {doc('export-old')}, {doc('export')}" in text


def test_frontmatter_status_caps_the_samples():
    samples = [{"code": f"usecase:KB-{i:02}", "places": ["doc:a", "doc:b"]} for i in range(15)]
    status = {
        "project": "p",
        "harness": "h",
        "home": "k",
        "ok": True,
        "sources": [],
        "graph": {"ready": False},
        "multiple_definitions": {"codes": 17, "samples": samples, "hint": "use derived_from"},
    }
    text = render_status(status)
    assert "codes defined in two or more places: 17; use derived_from" in text
    assert text.count("doc:a, doc:b") == 15
    assert "        and 2 more" in text


def test_frontmatter_follows_identifier_changes(tmp_path, kg_env):
    """The structure task's memo key covers the identifiers: a new pattern reaches unchanged documents."""
    project = make_project(tmp_path, kg_env)
    root = project.harness.root
    write(root / "screens.md", "---\nid: SCR-9\n---\n\n# Results screen\n")
    first = sync_build(project)
    store = Store.for_project(project)
    assert store.node("screen:SCR-9", first.build_id) is None

    write(root / "knowledge.yaml", (root / "knowledge.yaml").read_text() + SCREEN)
    project = load_project_at(root, project.home)
    second = build_project(project, verify=True, cold=True)
    assert second.ok, second.errors
    edges = live_edges(Store.for_project(project), second.build_id)
    assert edges[("harness:file:screens.md", "defines", "screen:SCR-9")] == "parsed"

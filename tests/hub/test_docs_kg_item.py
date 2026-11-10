"""The kg/1 items of the feature docs' source (``evo_agents.hub.server.docs.kg``), as the hub checks a source's
answer (``evo_agents.hub.kg_sources.checked``): no database, made-up documents of a project northwind."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub.kg_sources import checked
from evo_agents.hub.server.docs.kg import document_item
from evo_agents.hub.server.docs.store import KgDocument

SOURCE = "plugin.docs"
KNOWLEDGE = {"policy": {"levels": ["public", "internal", "secret"]}}
SAVED = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)


def doc(slug: str = "lumen-notes", body: str = "Intro.\n\n## Setup\n\nRun the lumen board.\n", **label) -> KgDocument:
    return KgDocument(slug, "Lumen notes", {"level": "internal", **label}, 3, "alice", body, SAVED)


def test_a_document_is_a_doc_item_with_its_body_its_sections_its_version_and_author():
    item = document_item(SOURCE, doc(), ("https://hub.test/", "northwind"))
    assert item["id"] == "plugin.docs:document:lumen-notes" and item["kind"] == "doc"
    assert item["props"] == {"document": "lumen-notes", "version": 3, "author": "alice"}
    assert item["body"] == {"format": "markdown", "text": doc().body}
    assert [fragment["anchor"] for fragment in item["fragments"]] == ["_preamble", "setup"]
    assert item["uri"] == "https://hub.test/p/northwind/x/docs/documents/lumen-notes"
    assert item["rev_time"] == "2026-11-08T09:30:00.000000Z"
    (kept,) = checked(SOURCE, "docs", {"items": [item]}, KNOWLEDGE, "northwind").items
    assert kept["label"] == {"level": "internal", "integrity": "U"}


def test_an_empty_document_has_no_text_and_a_label_of_another_project_keeps_it_out():
    empty = document_item(SOURCE, doc(body="  \n"))
    assert "body" not in empty and "fragments" not in empty and "uri" not in empty
    other = document_item(SOURCE, doc("shared", projects=["northwind", "lumen"]))
    found = checked(SOURCE, "docs", {"items": [empty, other]}, KNOWLEDGE, "northwind")
    assert [item["id"] for item in found.items] == ["plugin.docs:document:lumen-notes"] and found.left_out == 1


def test_an_html_page_gives_the_graph_its_text_with_its_headings_and_no_script():
    page = (
        "<title>Board</title><style>h1{}</style><h1>Lumen board</h1><p>Read the <b>board</b>.</p><script>x()</script>"
    )
    item = document_item(SOURCE, KgDocument("board", "Board", {"level": "internal"}, 1, "alice", page, SAVED, "html"))
    assert item["body"] == {"format": "markdown", "text": "Board\n\n# Lumen board\n\nRead the board."}
    assert [fragment["anchor"] for fragment in item["fragments"]] == ["_preamble", "lumen-board"]
    scripted = KgDocument("app", "App", {"level": "internal"}, 1, "alice", "<script>draw()</script>", SAVED, "html")
    assert "body" not in document_item(SOURCE, scripted)

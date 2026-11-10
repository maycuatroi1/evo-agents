"""The HTML pages of the feature docs without a hub: the text the knowledge graph reads of one
(``evo_agents.hub.server.docs.page_text``), the title ``evo-agents hub docs put`` gives one (``cli_docs.page_title``),
and whether a page of a project loads libraries from the CDNs (``store.cdn_allowed``), by the project's hub sink."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo_agents.hub.cli_docs import page_title

LADDER = SimpleNamespace(levels=["public", "internal", "customer", "secret"], locations=["any", "domestic-only"])

PAGE = """<!doctype html>
<html><head><title>Lumen &amp; board</title><style>h1 { color: red }</style></head>
<body>
  <h1>Lumen   board</h1>
  <p>Read the <b>board</b> first.<br>Then the queue.</p>
  <script>document.body.append("never read")</script>
  <template><p>Not shown</p></template>
  <svg viewBox="0 0 10 10"><g><text>portal-web</text></g><g><text>ops-api</text></g></svg>
  <ul><li>one</li><li>two</li></ul>
  <h3>Notes</h3><div>Kept.</div>
</body></html>
"""


def test_the_graph_reads_the_text_a_reader_sees_without_a_script():
    pytest.importorskip("sqlalchemy", reason="the feature docs need the hub-server extra, evo-ak[hub-server]")
    from evo_agents.hub.server.docs.page_text import page_text

    assert page_text(PAGE) == (
        "# Lumen board\n\nRead the board first.\n\nThen the queue.\n\nportal-web\n\nops-api\n\none\n\ntwo\n\n"
        "### Notes\n\nKept."
    )
    assert page_text("<title>Fragment</title><h2>Only</h2>") == "Fragment\n\n## Only"
    assert page_text("<head><title>Open head</title><body><p>Body text") == "Body text"
    assert page_text("<script>draw()</script>") == ""


def test_put_takes_a_pages_title_from_its_title_element():
    assert page_title(PAGE) == "Lumen & board"
    assert page_title("<TITLE>\n  Two\n  lines </TITLE>") == "Two lines"
    assert page_title("<p>No title</p>") is None and page_title("<title> </title>") is None


@pytest.mark.parametrize(
    ("level", "location", "allowed"),
    [
        ("public", "any", True),
        ("internal", "any", True),
        ("customer", "any", False),
        ("secret", "any", False),
        ("public", "domestic-only", False),
    ],
)
def test_a_page_loads_from_the_cdns_only_in_a_project_that_is_not_restricted(level, location, allowed):
    pytest.importorskip("sqlalchemy", reason="the hub's store needs the hub-server extra, evo-ak[hub-server]")
    from evo_agents.hub.server.docs.store import cdn_allowed

    sink = SimpleNamespace(level=LADDER.levels.index(level), location=LADDER.locations.index(location))
    rules = SimpleNamespace(levels=LADDER.levels, locations=LADDER.locations, hub_sink=sink)
    assert cdn_allowed(SimpleNamespace(rules=rules)) is allowed
    assert cdn_allowed(SimpleNamespace(rules=SimpleNamespace(**{**vars(rules), "hub_sink": None}))) is False

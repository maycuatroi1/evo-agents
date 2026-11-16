"""The rows of a build made in memory (``evo_agents.kg.graph_rows``): the same rows for the same corpus, the asked
index's nodes and the texts semantic search embeds."""

from evo_agents.kg.build import build_rows, unchanged_rows
from evo_agents.kg.graph_rows import RowsVectors, asked_nodes, with_asked
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc


def test_rows_built_in_memory_are_the_same_for_the_same_corpus(fake_project):
    project, fake = fake_project
    fake.set(
        items=[
            doc("guide", "# Guide\n\nHow the app works, see setup.\n\n## Install\n\nRun the installer.\n"),
            doc("setup", "# Setup\n\nInstall it.\n"),
        ]
    )
    assert all(r.ok for r in sync_project(project))
    report, rows = build_rows(project)
    assert report.ok and rows is not None and not report.skipped
    again, twin = build_rows(project)
    assert twin is not None and rows.content_hash == twin.content_hash == again.content_hash == report.content_hash
    assert sorted(rows.nodes) == sorted(twin.nodes) and sorted(rows.edges) == sorted(twin.edges)
    assert (rows.keyed, rows.code, rows.top) == (twin.keyed, twin.code, twin.top)
    assert (rows.units, rows.wheres, rows.derivations) == (twin.units, twin.wheres, twin.derivations)
    assert (rows.clusters, rows.review) == (twin.clusters, twin.review)
    nodes = asked_nodes(rows)
    assert nodes.known == {row[0] for row in rows.nodes}
    assert all(set(ids) <= nodes.known for ids in nodes.paths.values())
    embedded = RowsVectors(rows).rows(200)
    assert embedded == sorted(embedded) and any("Run the installer" in text for *_, text in embedded)
    held, digest = with_asked(rows, [], 0.5)
    assert held.asked == () and held.asked_weight == 0.5 and digest is None


def test_rows_of_the_corpus_state_and_inputs_the_project_has_skip_the_build(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    assert all(r.ok for r in sync_project(project))
    _, rows = build_rows(project)
    again = unchanged_rows(project, rows)
    assert again is not None and again.skipped and again.content_hash == rows.content_hash
    fake.set(items=[doc("a"), doc("b")])
    assert all(r.ok for r in sync_project(project))
    assert unchanged_rows(project, rows) is None and unchanged_rows(project, None) is None

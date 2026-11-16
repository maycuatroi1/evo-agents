"""The rows of a build made in memory (``evo_agents.kg.graph_rows``) are those a store file reads back."""

from evo_agents.kg import store_vectors
from evo_agents.kg.asked import Nodes
from evo_agents.kg.build import build_project, build_rows, unchanged_rows
from evo_agents.kg.graph_rows import RowsVectors, asked_nodes, with_asked
from evo_agents.kg.snapshot import store_rows
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc

PLACED = ("build_id", "built_at", "file_bytes")  # what the keeper of the rows, or the file, gives a build


def without(summary: dict) -> dict:
    stats = {k: v for k, v in summary["stats"].items() if k not in ("memo", "state_digest")}
    return {**{k: v for k, v in summary.items() if k not in PLACED}, "stats": stats}


def test_rows_built_in_memory_are_those_the_store_reads_back(fake_project):
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
    stored = build_project(project, force=True)
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        kept = store_rows(store, b)
        assert rows.content_hash == kept.content_hash == stored.content_hash == report.content_hash
        assert sorted(rows.nodes) == sorted(kept.nodes) and sorted(rows.edges) == sorted(kept.edges)
        assert (rows.keyed, rows.code, rows.top) == (kept.keyed, kept.code, kept.top)
        assert (rows.units, rows.wheres, rows.derivations) == (kept.units, kept.wheres, kept.derivations)
        assert (rows.clusters, rows.review) == (kept.clusters, kept.review)
        assert without(rows.summary) == without(kept.summary)
        on_file, in_memory = Nodes.of_store(store, b), asked_nodes(rows)
        assert (in_memory.known, dict(in_memory.paths)) == (on_file.known, dict(on_file.paths))
        assert RowsVectors(rows).rows(200) == store_vectors.index_rows(store.db, 200)
        held, digest = with_asked(rows, [], 0.5)
        assert held.asked == () and held.asked_weight == 0.5 and digest is None
    finally:
        store.close()


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

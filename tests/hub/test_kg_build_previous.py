"""What a hub build holds of the graph it follows (``kg_build.build_graph``): its head and clusters only, the whole of
its rows read only for a build that builds nothing; and ``write_graph`` makes the rows of one table at a time
(``kg_graph_rows.table_rows``). Holding both at once had a build of some 25,000 items past a kg worker's memory cap."""

from dataclasses import replace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import kg_build, kg_graph_rows
from evo_agents.kg.build import build_rows
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc, fake_project, kg_env  # noqa: F401 (fixtures)


def head_of(rows):
    return replace(rows, nodes=(), edges=(), keyed=(), code={}, units=(), wheres=(), derivations=(), vectors=())


def test_a_build_reads_the_whole_previous_graph_only_when_it_builds_nothing(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("guide", "# Guide\n\nHow the app works, see setup.\n"), doc("setup", "# Setup\n\nRun it.\n")])
    assert all(r.ok for r in sync_project(project))
    _, rows = build_rows(project)
    reads = []

    def whole():
        reads.append(1)
        return rows

    kept, report = kg_build.build_graph(project, previous=head_of(rows), whole=whole)
    assert report.skipped and kept is rows and reads == [1]

    stale = replace(rows, content_hash="sha256:other")  # the graph moved on since the head was read
    built, report = kg_build.build_graph(project, previous=head_of(rows), whole=lambda: stale)
    assert not report.skipped and built.content_hash == rows.content_hash

    fake.set(items=[doc("guide", "# Guide\n\nEdited.\n"), doc("setup", "# Setup\n\nRun it.\n")])
    assert all(r.ok for r in sync_project(project))
    reads.clear()
    built, report = kg_build.build_graph(project, previous=head_of(rows), whole=whole)
    assert not report.skipped and reads == [] and built.content_hash != rows.content_hash


def test_the_rows_of_each_table_are_those_of_the_whole_graph(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("guide", "# Guide\n\nHow the app works, see setup.\n"), doc("setup", "# Setup\n\nRun it.\n")])
    assert all(r.ok for r in sync_project(project))
    _, rows = build_rows(project)
    every = kg_graph_rows.graph_rows(rows)
    assert list(every) == list(kg_graph_rows.WRITE_ORDER) and set(every) == set(kg_graph_rows.GRAPH_TABLES)
    assert all(kg_graph_rows.table_rows(rows, name) == every[name] for name in every)
    assert every["kg_nodes"] and every["kg_text_rows"] and every["kg_provenance"]
    with pytest.raises(ValueError):
        kg_graph_rows.table_rows(rows, "kg_corpus_items")

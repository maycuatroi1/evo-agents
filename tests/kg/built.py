"""Graphs the tests build in memory, as the hub builds them (``evo_agents.kg.build.build_rows``): no store file. The
rows of a project's latest build are kept here by its directory; a build whose corpus state and inputs did not change
is skipped (``unchanged_rows``), and the next one starts its clustering from the clusters of the one before, as the
hub's ``build_graph``. The vectors the project's semantic settings ask for are put into the rows as the hub puts them
(``evo_agents.hub.kg_semantic.index``). Read a build with ``snapshot_of``, or ask it through the hub's engine with
``tests.kg.hub_graph.hub_session``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="a hub build needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.kg import semantic  # noqa: E402
from evo_agents.kg.build import BuildReport, build_rows, tuning_of, unchanged_rows, verify_rows  # noqa: E402
from evo_agents.kg.clusters import Clusters  # noqa: E402
from evo_agents.kg.project import Project, ProjectError  # noqa: E402
from evo_agents.kg.snapshot import Snapshot, SnapshotView, StoreRows, edge_dict, node_dict, rows_clusters  # noqa: E402
from evo_agents.kg.sync import sync_project  # noqa: E402


@dataclass
class Held:
    rows: StoreRows
    warm: Clusters | None  # the clusters the build started from, for a verifying build of it
    number: int  # the project's builds so far: the build's id
    cache: semantic.MemoryCache = field(default_factory=semantic.MemoryCache)


HELD: dict[Path, Held] = {}


def build(
    project: Project,
    *,
    force: bool = False,
    verify: bool = False,
    cold: bool = False,
    entity_outbox: list | None = None,
    entity_inbox: dict | None = None,
    entity_keys: dict | None = None,
) -> BuildReport:
    """Build ``project`` in memory and keep its rows; a build of what the kept rows were built from is skipped unless
    ``force``. ``verify`` rebuilds and compares content hashes (``verify_rows``), ``cold`` with an empty memo."""
    held = HELD.get(project.root)
    report = None if force else unchanged_rows(project, held.rows if held else None)
    if report is None:
        warm = rows_clusters(held.rows.clusters) if held else None
        report, rows = build_rows(
            project, warm=warm, entity_outbox=entity_outbox, entity_inbox=entity_inbox, entity_keys=entity_keys
        )
        if rows is None:
            return report
        held = Held(rows, warm, held.number + 1 if held else 1, held.cache if held else semantic.MemoryCache())
        HELD[project.root] = held
        _vectors(project, held, report)
    report.build_id = held.number
    if verify and report.ok:
        report.verify = verify_rows(project, report.content_hash, cold=cold, warm=held.warm)
        report.ok = report.verify["match"]
    return report


def _vectors(project: Project, held: Held, report: BuildReport) -> None:
    from evo_agents.hub import kg_semantic

    try:
        settings = tuning_of(project).semantic_settings()
    except (ValueError, ProjectError) as exc:
        report.warnings.append(f"semantic: {exc}")
        return
    held.rows, found = kg_semantic.index(held.rows, settings, held.cache)
    if found.error:
        report.warnings.append(found.line())


def sync_build(project: Project, **kw) -> BuildReport:
    """Sync every source of ``project``, then ``build`` it; both must succeed."""
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    report = build(project, **kw)
    assert report.ok, report.errors
    return report


def rows_of(project: Project) -> StoreRows | None:
    """The rows of the project's latest build; None before one."""
    held = HELD.get(project.root)
    return held.rows if held else None


def snapshot_of(project: Project) -> Snapshot | None:
    """The snapshot of the project's latest build, numbered as the project's builds; None before one."""
    held = HELD.get(project.root)
    return Snapshot.from_stored(held.rows, held.number) if held else None


def nodes_of(project: Project) -> list[dict]:
    """The nodes of the project's latest build as a reader of the graph reads them (``node_dict``), by id; [] before a build."""
    rows = rows_of(project)
    return sorted((node_dict(r) for r in rows.nodes), key=lambda n: n["id"]) if rows is not None else []


def edges_of(project: Project) -> list[dict]:
    """The edges of the project's latest build as a reader of the graph reads them (``edge_dict``), an edge to no node included, by
    (src, rel, dst); [] before a build."""
    rows = rows_of(project)
    return (
        sorted((edge_dict(r) for r in rows.edges), key=lambda e: (e["src"], e["rel"], e["dst"]))
        if rows is not None
        else []
    )


def view_of(project: Project) -> SnapshotView | None:
    """The latest build as a reader with no audience reads it, through the reads of a ``SnapshotView`` (``b`` ignored); None
    before a build."""
    snap = snapshot_of(project)
    return snap.view() if snap is not None else None


def old_vector_cache(path: Path, model: str, rows: dict[str, bytes]) -> Path:
    """A ``vectors.sqlite`` as builds kept next to their graph file before the hub's kg_vector_cache: the vectors
    ``rows`` of ``model`` by digest of their text, which ``sqlite_legacy.vector_cache_rows`` reads. The hub's tests
    write theirs here, where the raw SQL guard of the hub does not look."""
    import sqlite3

    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE vec (model TEXT, digest TEXT, vec BLOB, PRIMARY KEY (model, digest))")
        db.executemany("INSERT INTO vec VALUES (?, ?, ?)", [(model, d, v) for d, v in sorted(rows.items())])
        db.commit()
    finally:
        db.close()
    return path

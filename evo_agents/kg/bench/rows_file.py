"""A bench graph as the bench keeps it: the rows of a build made in memory (``build.build_rows``), pickled and
compressed into ROWS in the graph's project, no graph file, and its summary into SUMMARY, which a profile reads
without the rows. ``RowsSession`` asks the tools of ``kg serve`` over the
snapshot of those rows, ``VectorFile`` keeps the vectors of a repo's snapshots in a file of their own, and
``local_snapshot`` builds a project of this machine in memory for a local question set.

A snapshot read from ROWS is kept while its file does not change, so the tasks asked of one graph read it once."""

from __future__ import annotations

import json
import os
import pickle
import zlib
from dataclasses import replace
from pathlib import Path

from evo_agents.kg import semantic
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.project import Project
from evo_agents.kg.serve import Session
from evo_agents.kg.snapshot import Snapshot, SnapshotView, StoreRows

ROWS = "graph.rows"
SUMMARY = "graph.summary.json"
ROWS_FORMAT = 1  # of the file: a change builds the bench's graphs again (``graphs.extractor``)
LEVEL = 1  # zlib's fastest, as ``evo_agents.kg.packed``
BUILD = 1  # the build id a bench graph's summary names: one build per graph
UNREADABLE = (OSError, ValueError, zlib.error, pickle.UnpicklingError, EOFError)

_held: dict[str, tuple[tuple[int, int], Snapshot]] = {}  # the last snapshot read, by its file and that file's stamp


def _write(path: Path, value) -> None:
    """Pickle and compress ``value`` into ``path`` at once: a reader finds the file whole or not at all."""
    _replace(path, zlib.compress(pickle.dumps(value, pickle.HIGHEST_PROTOCOL), LEVEL))


def _replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_name(f"{path.name}.{os.getpid()}")
    spare.write_bytes(data)
    os.replace(spare, path)


def _read(path: Path):
    return pickle.loads(zlib.decompress(path.read_bytes()))


def write_rows(project_dir: Path, rows: StoreRows) -> None:
    """Keep ``rows`` as the graph of the project in ``project_dir``, its summary beside them."""
    _replace(project_dir / SUMMARY, json.dumps(rows.summary).encode())
    _write(project_dir / ROWS, rows)


def summary_of(project_dir: Path) -> dict | None:
    """The summary of the graph the project in ``project_dir`` keeps; None without one."""
    if not (project_dir / ROWS).is_file():
        return None
    try:
        found = json.loads((project_dir / SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def read_rows(project_dir: Path) -> StoreRows:
    """The rows the graph of the project in ``project_dir`` keeps; BenchError when it keeps none."""
    try:
        return _read(project_dir / ROWS)
    except UNREADABLE as exc:
        raise BenchError(f"{project_dir.name} has no graph: {project_dir / ROWS} does not read ({exc})") from None


def build(project: Project, project_dir: Path | None = None, memo=None):
    """Build ``project`` in memory with ``memo`` (the project's own when None): the report and the rows, numbered
    BUILD, kept in ``project_dir`` when given and the build succeeded."""
    from evo_agents.kg.build import build_rows

    report, rows = build_rows(project, memo=memo)
    if rows is not None:
        rows = replace(rows, summary={**rows.summary, "build_id": BUILD})
        report.build_id = BUILD
        if project_dir is not None:
            write_rows(project_dir, rows)
    return report, rows


def snapshot_of(project_dir: Path) -> Snapshot:
    """The snapshot of the graph the project in ``project_dir`` keeps, read again only when its file changed."""
    path = project_dir / ROWS
    try:
        st = path.stat()
    except OSError:
        raise BenchError(f"{project_dir.name} has no graph: no {path}") from None
    stamp = (st.st_mtime_ns, st.st_size)
    found = _held.get(str(path))
    if found is not None and found[0] == stamp:
        return found[1]
    _held.clear()  # one graph at a time: a bench graph can take GBs in memory
    snap = Snapshot.from_stored(read_rows(project_dir), BUILD)
    _held[str(path)] = (stamp, snap)
    return snap


def forget() -> None:
    """Let go of the snapshot kept, as before its graph is deleted."""
    _held.clear()


def local_snapshot(project: Project) -> Snapshot:
    """The snapshot of a build in memory of ``project``'s corpus on this machine, for a local question set."""
    report, rows = build(project)
    if rows is None:
        raise BenchError(f"project {project.name} does not build: {'; '.join(report.errors[:2]) or 'no graph'}")
    return Snapshot.from_stored(rows, BUILD)


def tuning_of(project: Project) -> kg_settings.Tuning:
    """What search is tuned by: the override asked (``kg bench --settings``), else the project's settings.json, else
    the defaults."""
    asked = kg_settings.override_of()
    if asked is not None:
        return asked
    try:
        return kg_settings.read(project.root)
    except kg_settings.SettingsError:
        return kg_settings.DEFAULT


class RowsSession(Session):
    """A session of ``kg serve`` over ``snap``, a graph the bench holds in memory, tuned as ``tuning_of`` says."""

    def __init__(self, project: Project, sink: str, snap: Snapshot):
        super().__init__(project, sink)
        self.snap = snap

    def view(self) -> SnapshotView:
        assert self.project is not None
        return self.snap.view(tuning=tuning_of(self.project))

    def _graph(self) -> SnapshotView:
        view = self.view()
        if view.latest_ready() is None:
            raise self.no_graph()
        return view


class VectorFile(semantic.MemoryCache):
    """A ``semantic.Cache`` kept in a file: read when made, written back by ``close`` when it changed. One process
    writes a repo's file at a time (a bench run asks a repo's tasks in one process)."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        try:
            self.held = _read(path)
        except UNREADABLE:
            self.held = {}
        self.saved = dict(self.held)

    def close(self) -> None:
        if self.held != self.saved:
            _write(self.path, self.held)
            self.saved = dict(self.held)

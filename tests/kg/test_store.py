import sqlite3
from pathlib import Path

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import SCHEMA_VERSION, StaleSchema, Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

APP_SOURCE = "  - id: app\n    connector: git\n    repo: app\n"


def sync_build(project, **kw):
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    report = build_project(project, **kw)
    assert report.ok, report.errors
    return report


def edit_sources(project, old: str, new: str):
    text = project.knowledge_path.read_text(encoding="utf-8")
    assert old in text
    write(project.knowledge_path, text.replace(old, new))
    return load_project_at(project.harness.root, project.home)


def live_keys(store: Store, b: int) -> dict[str, str]:
    rows = store.db.execute(
        "SELECT node_id, json_extract(props, '$.key') FROM nodes WHERE kind = 'Symbol'"
        " AND tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)",
        {"b": b},
    )
    return dict(rows.fetchall())


def make_schema_1(path) -> None:
    """Turn a store file into what schema 1 wrote: the same tables without the key index."""
    db = sqlite3.connect(path)
    db.execute("DROP INDEX nodes_key")
    db.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    db.commit()
    db.close()


def test_pg_key_names_every_symbol_by_repo_path_and_qualname(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    node = store.node("symbol:app:app/server.py::Server.handle", report.build_id)
    assert node["props"]["key"] == ["app", "app/server.py", "Server.handle"]
    keys = live_keys(store, report.build_id)
    assert keys and all(k is not None for k in keys.values())
    assert len(set(keys.values())) == len(keys)


def test_pg_key_index_refuses_a_second_live_holder(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    b = report.build_id
    columns = "node_id, kind, name, props, status, conf, label_lvl, label_loc, label_int, aliases, content, tx_from"
    original = store.db.execute(
        f"SELECT {columns} FROM nodes WHERE node_id = 'symbol:app:app/search.py::search' AND tx_to IS NULL"
    ).fetchone()
    twin = ("symbol:other:app/search.py::search", *original[1:])
    with pytest.raises(sqlite3.IntegrityError, match="nodes_key"):
        store.db.execute(f"INSERT INTO nodes ({columns}) VALUES ({', '.join('?' * 12)})", twin)
    store.db.rollback()
    # A proposed node or a closed row may share the key: the index covers live, non-proposed rows only.
    proposed = (*twin[:4], "proposed", *twin[5:])
    store.db.execute(f"INSERT INTO nodes ({columns}) VALUES ({', '.join('?' * 12)})", proposed)
    store.db.execute(
        f"INSERT INTO nodes ({columns}, tx_to) VALUES ({', '.join('?' * 13)})",
        ("symbol:old:app/search.py::search", *original[1:], b),
    )
    store.db.rollback()


def test_pg_key_duplicate_fails_the_build_and_names_the_sources(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    good = sync_build(project)
    # A second source over the same repo: every symbol it reads already has a holder.
    project = edit_sources(project, APP_SOURCE, APP_SOURCE + "  - id: app-copy\n    connector: git\n    repo: app\n")
    assert all(r.ok for r in sync_project(project))
    bad = build_project(project)
    assert not bad.ok
    line = next(e for e in bad.errors if '["app", "app/search.py", "search"]' in e)
    assert line.startswith("duplicate Symbol key")
    assert "symbol:app:app/search.py::search (source app)" in line
    assert "symbol:app-copy:app/search.py::search (source app-copy)" in line
    assert "duplicate Symbol key" in bad.summary_line()
    assert Store.for_project(project).latest_ready() == good.build_id


def test_pg_key_holder_can_change_between_builds(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    first = sync_build(project)
    store = Store.for_project(project)
    before = live_keys(store, first.build_id)

    # Same key, new content: the old row closes before the new one opens.
    text = (app / "app/search.py").read_text(encoding="utf-8")
    write(app / "app/search.py", "# moved down a line\n" + text)
    commit(app)
    edited = sync_build(project, verify=True)
    assert edited.verify["match"], edited.verify
    assert store.node("symbol:app:app/search.py::search", edited.build_id)["props"]["line"] == 6

    # Same key, new node id: the source is renamed but still reads repo app.
    project = edit_sources(project, APP_SOURCE, APP_SOURCE.replace("id: app", "id: app2"))
    renamed = sync_build(project, verify=True, cold=True)
    assert renamed.verify["match"], renamed.verify
    after = live_keys(store, renamed.build_id)
    assert sorted(after.values()) == sorted(before.values())
    assert all(nid.startswith("symbol:app2:") for nid in after)


def test_schema_upgrade_rebuilds_from_the_log_and_replaces_the_file(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    first = sync_build(project)
    second = sync_build(project)  # two builds, so the old file has history the rebuild drops
    path = project.root / "graph.sqlite"
    make_schema_1(path)

    report = build_project(project, verify=True)
    assert report.ok, report.errors
    assert report.rebuilt_from == "1"
    assert "store rebuilt from the log (schema 1 -> 2)" in report.summary_line()
    assert report.verify["match"], report.verify
    assert report.content_hash == first.content_hash == second.content_hash
    assert Store.stored_schema(path) == SCHEMA_VERSION
    store = Store.for_project(project)
    assert store.latest_ready() == report.build_id == 1
    assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes_key'").fetchone()
    assert sorted(p.name for p in path.parent.glob("graph.sqlite*") if not p.name.endswith(("-wal", "-shm"))) == [
        "graph.sqlite"
    ]

    again = sync_build(project)
    assert again.rebuilt_from is None
    assert again.build_id == 2 and again.content_hash == report.content_hash


def test_schema_upgrade_is_refused_by_readers_and_keeps_the_file_on_failure(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_build(project)
    path = project.root / "graph.sqlite"
    make_schema_1(path)

    with pytest.raises(StaleSchema, match="schema 1, this code reads 2"):
        Store.open_existing(project)
    answer = Session(project, "agent").call("kg_search", {"query": "search"})
    assert answer["isError"] and "evo-agents kg build" in answer["content"][0]["text"]

    # A build that fails leaves the old file as it was: nothing is migrated in place.
    write(
        project.harness.root / "bindings/core.yaml",
        "bindings:\n  - from: app:app/search.py::missing\n    rel: implements\n    to: usecase:KB-01\n",
    )
    sync_project(project)
    bad = build_project(project)
    assert not bad.ok and bad.rebuilt_from is None
    assert Store.stored_schema(path) == "1"
    db = sqlite3.connect(path)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes_key'").fetchone() is None
    db.close()
    assert not (path.parent / "graph.sqlite.rebuild").exists()


def test_kind_counts_split_the_live_nodes_by_kind_and_label(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    rows = store.kind_counts(report.build_id)
    store.close()
    assert sum(count for *_, count in rows) == report.nodes
    assert len({tuple(row[:4]) for row in rows}) == len(rows)
    symbols = [row for row in rows if row[0] == "Symbol"]
    assert symbols and all(isinstance(count, int) and count > 0 for *_, count in symbols)


def test_leave_wal_leaves_the_whole_store_in_its_file(tmp_path):
    path = tmp_path / "graph.sqlite"
    store = Store(path)
    store.db.execute("INSERT INTO meta VALUES ('note', 'kept')")
    store.db.commit()
    assert Path(f"{path}-wal").exists()
    store.leave_wal()
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    store.close()
    assert not Path(f"{path}-wal").exists()
    reopened = sqlite3.connect(path)
    assert reopened.execute("SELECT value FROM meta WHERE key = 'note'").fetchone() == ("kept",)
    reopened.close()

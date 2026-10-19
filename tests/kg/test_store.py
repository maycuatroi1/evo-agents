import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from evo_agents.kg import retention
from evo_agents.kg import search as text_search
from evo_agents.kg import store as store_module
from evo_agents.kg.build import build_project, memo_path, verify_build
from evo_agents.kg.ids import utc_text
from evo_agents.kg.memo import UNUSED
from evo_agents.kg.pipeline.stages import leading_comment
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import SCHEMA_VERSION, StaleSchema, Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, make_project, write

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
    second = sync_build(project, force=True)  # two builds, so the old file has history the rebuild drops
    path = project.root / "graph.sqlite"
    make_schema_1(path)

    report = build_project(project, verify=True)
    assert report.ok, report.errors
    assert report.rebuilt_from == "1"
    assert f"store rebuilt from the log (schema 1 -> {SCHEMA_VERSION})" in report.summary_line()
    assert report.verify["match"], report.verify
    assert report.content_hash == first.content_hash == second.content_hash
    assert Store.stored_schema(path) == SCHEMA_VERSION
    store = Store.for_project(project)
    assert store.latest_ready() == report.build_id == 1
    assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes_key'").fetchone()
    assert sorted(p.name for p in path.parent.glob("graph.sqlite*") if not p.name.endswith(("-wal", "-shm"))) == [
        "graph.sqlite"
    ]

    again = sync_build(project, force=True)
    assert again.rebuilt_from is None
    assert again.build_id == 2 and again.content_hash == report.content_hash


def test_schema_upgrade_is_refused_by_readers_and_keeps_the_file_on_failure(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_build(project)
    path = project.root / "graph.sqlite"
    make_schema_1(path)

    with pytest.raises(StaleSchema, match=f"schema 1, this code reads {SCHEMA_VERSION}"):
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


# Search: the full-text index and the lookups by id and name (evo_agents.kg.search)

HUB_BUILD = '''"""Where the hub builds the knowledge graph from the corpus."""


class GraphBuilder:
    """Turns a corpus into nodes and edges."""

    def buildGraph(self, corpus):
        return corpus


def load_corpus(path):
    return path
'''
NEEDLE = "zanzibarquokka"  # a word only past the first 4,000 characters of the long section


def make_searchable(tmp_path, kg_env, **kw):
    """The demo project plus a module with docstrings and camelCase names, a Vietnamese note and a long section."""
    project = make_project(tmp_path, kg_env, **kw)
    app = tmp_path / "demo-app"
    write(app / "app/hub_build.py", HUB_BUILD)
    write(app / "docs/do-thi.md", "# Đồ thị tri thức\n\nĐường đi của dữ liệu qua các nguồn.\n")
    write(app / "docs/plain.md", "# Ghi chu\n\nDuong ong du lieu chay moi dem.\n")
    write(app / "docs/long.md", "# Long\n\n" + "Filler sentence about nothing. " * 200 + f"\n\n{NEEDLE} at the end.\n")
    commit(app, "searchable")
    return project


def found(store: Store, query: str, **kw) -> list[str]:
    return store.search(query, store.latest_ready(), **kw)


def test_search_ranks_any_word_when_no_node_holds_them_all(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    question = "where does the hub build the knowledge graph on tuesdays"
    assert found(store, question, any_word=False) == []  # no node holds every word: AND alone finds nothing
    hits = found(store, question, limit=5)
    assert hits[0] in ("app:file:app/hub_build.py", "symbol:app:app/hub_build.py::GraphBuilder")
    assert "app:file:app/hub_build.py" in hits[:5]


def test_search_splits_camel_case_reads_d_for_dj_and_indexes_docstrings(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    builder, method = (
        "symbol:app:app/hub_build.py::GraphBuilder",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
    )
    assert found(store, "GraphBuilder")[0] == builder  # its name: through the index, first
    assert builder in found(store, "graph builder")[:3]  # the parts of a camelCase name
    assert method in found(store, "buildgraph")[:3]  # the whole word too
    assert "symbol:app:app/hub_build.py::load_corpus" in found(store, "load corpus")[:3]  # snake_case
    assert "app:file:app/hub_build.py" in found(store, "hub_build")[:3]  # a path
    assert found(store, "corpus into nodes and edges")[0] == builder  # its docstring
    assert "app:file:docs/do-thi.md#đồ-thị-tri-thức" in found(store, "do thi tri thuc")[:3]  # đ reads as d
    assert "app:file:docs/plain.md#ghi-chu" in found(store, "đường ống dữ liệu")[:3]  # both ways
    assert any(h.startswith("app:file:docs/long.md") for h in found(store, NEEDLE))  # past 4,000 characters


def test_a_word_the_index_folds_into_an_english_stop_word_is_left_out_as_it_is():
    # The tokenizer folds accents: "án" is "an" in the index, so it would match every English "an".
    for typed in ("Phương án A", "phuong an a", "PHƯƠNG ÁN"):
        assert [w.lower() for w in text_search.query_words(typed)] == ["phương" if "ư" in typed.lower() else "phuong"]
    assert list(text_search.fts_queries(text_search.query_words("Phương án"))) == ['"phương"', '"phương"*']
    assert text_search.query_words("mã lỗi của bảng") == ["mã", "lỗi", "bảng"]  # "của" asks; the others say
    assert text_search.query_words("án") == ["án"]  # a query of nothing else keeps its words
    assert text_search.phrases('("hubsession" OR ("hub" AND "session")) AND "x"* OR "hub"') == [
        '"hubsession"',
        '"hub"',
        '"session"',
        '"x"*',
    ]


def test_search_keeps_uris_out_and_filters_kinds_in_the_query(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    assert found(store, "blob") == []  # every node's URI has /blob/main/: none of it is indexed
    sections = found(store, "search", limit=2, kinds=["Section"])
    assert sections and all(":file:docs/" in h and "#" in h for h in sections)
    assert found(store, "rank_results", kinds=["Section"]) == ["app:file:docs/guide.md#details"]
    answer = Session(project, "agent").call("kg_search", {"query": "search", "kinds": ["Section"], "limit": 1})
    assert [r["kind"] for r in answer["structuredContent"]["results"]] == ["Section"]


def test_a_text_past_the_cap_is_cut_and_counted_in_coverage(tmp_path, kg_env, monkeypatch):
    monkeypatch.setattr(text_search, "MAX_TEXT_CHARS", 3000)
    project = make_searchable(tmp_path, kg_env)
    report = sync_build(project)
    cut = report.coverage["search"]
    assert cut["cut"] == 1 and cut["samples"] == ["app:file:docs/long.md#long"] and cut["max_chars"] == 3000
    assert found(Store.for_project(project), NEEDLE) == []


def test_lookups_by_id_name_and_alias_go_through_an_index(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    b = store.latest_ready()
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    store.exact("symbol:app:app/search.py::search", b, 10)
    store.exact("search", b, 10, ["Symbol"])
    store.alias_of("usecase:KB-99", b)
    store.node("app:file:app/search.py", b)
    store.db.set_trace_callback(None)
    assert len(statements) == 4
    for sql in statements:
        plan = [row[3] for row in store.db.execute(f"EXPLAIN QUERY PLAN {sql}")]
        assert any("USING INDEX nodes_" in step for step in plan), (sql, plan)
        assert not any(step.startswith("SCAN") for step in plan), (sql, plan)


def make_schema_2(path) -> None:
    """Turn a store file into what schema 2 wrote: node_fts without kind and path, no name index, no aliases."""
    db = sqlite3.connect(path)
    db.execute("DROP INDEX nodes_name")
    db.execute("DROP TABLE node_alias")
    db.execute("DROP TABLE node_fts")
    db.execute("CREATE VIRTUAL TABLE node_fts USING fts5(node_id UNINDEXED, name, body)")
    db.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    db.commit()
    db.close()


def test_a_store_of_schema_2_is_rebuilt_by_the_next_build_and_refused_by_readers(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    first = sync_build(project)
    path = project.root / "graph.sqlite"
    make_schema_2(path)
    with pytest.raises(StaleSchema, match=f"schema 2, this code reads {SCHEMA_VERSION}"):
        Store.open_file(path)
    answer = Session(project, "agent").call("kg_search", {"query": "graph builder"})
    assert answer["isError"] and "evo-agents kg build" in answer["content"][0]["text"]

    report = sync_build(project)
    assert report.rebuilt_from == "2" and report.content_hash == first.content_hash
    assert "symbol:app:app/hub_build.py::GraphBuilder" in found(Store.for_project(project), "graph builder")


def test_the_content_hash_covers_the_schema_and_what_search_reads(tmp_path, kg_env, monkeypatch):
    project = make_searchable(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    b = report.build_id
    before = store.content_hash(b, "a")
    assert before != store.content_hash(b, "b")
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", "99")
    assert store.content_hash(b, "a") != before  # the hub never serves a file of one schema for another


def fts_rows(store: Store) -> dict[str, int]:
    return dict(store.db.execute("SELECT node_id, rowid FROM node_fts").fetchall())


def fts_content(store: Store) -> list[tuple]:
    return store.db.execute("SELECT node_id, kind, name, path, body FROM node_fts ORDER BY node_id").fetchall()


def test_a_build_rewrites_only_the_rows_of_the_full_text_index_that_changed(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    before = fts_rows(store)
    app = tmp_path / "demo-app"
    write(app / "docs/plain.md", "# Ghi chu\n\nDuong ong du lieu chay moi zebracorn.\n")
    commit(app, "plain")
    report = sync_build(project, verify=True, cold=True)
    assert report.verify["match"], report.verify

    after = fts_rows(store)
    moved = {nid for nid, rowid in after.items() if before.get(nid) != rowid}
    new_commit = f"app:commit:{git(app, 'rev-parse', 'HEAD').strip()}"  # the commit that added the file
    assert new_commit in moved and all(nid.startswith("app:file:docs/plain.md") for nid in moved - {new_commit})
    assert dict(store.db.execute("SELECT node_id, fts_rowid FROM node_fts_rows").fetchall()) == after
    assert any(nid.startswith("app:file:docs/plain.md") for nid in found(store, "zebracorn"))
    fresh = Store(tmp_path / "fresh.sqlite")
    assert build_project(project, store=fresh).content_hash == report.content_hash
    assert fts_content(store) == fts_content(fresh)  # the index holds what a store written whole holds

    # An index written before node_fts_rows existed is written again whole by the next build.
    with store.db:
        store.db.execute("DELETE FROM node_fts_rows")
    forced = build_project(project, force=True)
    assert forced.content_hash == report.content_hash
    assert fts_content(store) == fts_content(fresh)
    assert len(fts_rows(store)) == store.db.execute("SELECT count(*) FROM node_fts_rows").fetchone()[0]


def test_a_build_records_its_size_and_its_highest_label(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    report = sync_build(project)
    store = Store.for_project(project)
    stats = store.build_row(report.build_id)["stats"]
    assert stats["size"] == {"nodes": report.nodes, "edges": report.edges, "units": report.units}
    assert store.top_label(report.build_id) == (2, 0)  # customer, any location
    assert stats["inputs"].startswith("sha256:")


def test_leading_comments_are_the_docstrings_of_other_languages():
    lines = [
        "#!/usr/bin/env node",
        "// Routes of the app.",
        "import x from 'y'",
        "",
        "/**",
        " * Builds the graph.",
        " */",
        "@Injectable()",
        "export function buildGraph() {}",
        "#[derive(Debug)]",
        "struct Plain;",
    ]
    assert leading_comment(lines, None) == "Routes of the app."
    assert leading_comment(lines, 9) == "Builds the graph."
    assert leading_comment(lines, 11) is None
    assert leading_comment(lines, 3) == "Routes of the app."


# Retention of the history and of the memo (evo_agents.kg.retention)

KEEP_LOGS = {"logs": "1d"}


def notes_build(project, version: int, **kw):
    app = project.harness.root.parent / "demo-app"
    write(app / "docs/notes.md", f"# Notes\n\nVersion {version} of the notes, about app/search.py.\n")
    commit(app, f"notes {version}")
    return sync_build(project, **kw)


def test_history_retention_counts_then_deletes_what_no_kept_build_reads(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    reports = [notes_build(project, version) for version in range(4)]
    ids, latest = [r.build_id for r in reports], reports[-1]
    store = Store.for_project(project)
    tomorrow = utc_text(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
    assert store.history_floor(tomorrow) == ids[-2]  # the latest two stay, however old
    assert store.history_floor("") == ids[0]  # every build finished within the period
    closed = {
        table: store.db.execute(f"SELECT count(*) FROM {table} WHERE tx_to <= ?", (ids[-2],)).fetchone()[0]
        for table in store_module.HISTORY_TABLES
    }
    assert store.history(ids[-2]) == {**closed, "builds": 2} and closed["nodes"] > 0
    kept_counts = store.counts(ids[-2])

    young = retention.plan(project.name, project.root, {**KEEP_LOGS, "history": "1d"})
    assert young.history_floor == ids[0] and not any(young.history.values())
    found = retention.plan(project.name, project.root, {**KEEP_LOGS, "history": "0", "memo": "1d"})
    assert found.history_floor == ids[-2] and found.history == store.history(ids[-2])
    assert found.counts()["history"] == sum(closed.values()) and found.counts()["builds"] == 2
    assert [b for (b,) in store.db.execute("SELECT build_id FROM builds ORDER BY 1")] == ids  # counting deletes nothing

    deleted = retention.apply(found)
    assert deleted["history"] == sum(closed.values()) and deleted["builds"] == 2 and deleted["logs"] == 0
    store = Store.for_project(project)
    assert [b for (b,) in store.db.execute("SELECT build_id FROM builds ORDER BY 1")] == ids[-2:]
    assert store.counts(ids[-2]) == kept_counts  # the build before the latest still reads whole
    assert store.latest_ready() == latest.build_id
    assert verify_build(project, latest.content_hash, cold=True)["match"]
    assert build_project(project, force=True).content_hash == latest.content_hash  # the next build diffs as before


def memo_stages(path, cut: str) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in db.execute(f"SELECT stage FROM memo WHERE key IN ({UNUSED})", {"cut": cut}))
    finally:
        db.close()


def test_memo_retention_deletes_the_rows_no_build_uses_and_keeps_the_latest_build_s(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    notes_build(project, 0)
    app = tmp_path / "demo-app"
    write(
        app / "app/search.py",
        "def rank_results(items):\n    return sorted(items, reverse=True)\n\n\n"
        "def search(query):\n    return rank_results([query, query])\n",
    )
    report = notes_build(project, 1)
    path = memo_path(project)

    young = retention.plan(project.name, project.root, {**KEEP_LOGS, "memo": "1d"})
    assert young.memo == 0 and young.memo_cut is not None and young.memo_rows > 0
    found = retention.plan(project.name, project.root, {**KEEP_LOGS, "memo": "0"})
    assert 0 < found.memo < found.memo_rows
    unused = memo_stages(path, found.memo_cut)
    assert len(unused) == found.memo and {"map", "link", "link:reads"} <= set(unused)  # the old texts' rows

    deleted = retention.apply(found)
    assert deleted["memo"] == found.memo
    assert retention.plan(project.name, project.root, {**KEEP_LOGS, "memo": "0"}).memo == 0
    db = sqlite3.connect(path)
    assert db.execute("SELECT count(*) FROM memo").fetchone()[0] == found.memo_rows - found.memo
    assert db.execute("SELECT count(*) FROM memo_used WHERE key NOT IN (SELECT key FROM memo)").fetchone()[0] == 0
    db.close()
    warm = build_project(project, force=True)  # every row the latest build used is still there
    assert warm.content_hash == report.content_hash
    assert all(stage["misses"] == 0 for stage in warm.memo.values()), warm.memo
    assert verify_build(project, report.content_hash, cold=True)["match"]


def test_memo_rows_from_before_uses_were_recorded_go_once_a_build_recorded_its_own(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    notes_build(project, 0)
    report = notes_build(project, 1)
    path = memo_path(project)
    db = sqlite3.connect(path)
    with db:
        db.execute("DELETE FROM memo_used")
        db.execute("DELETE FROM memo_meta")
    db.close()
    legacy = retention.plan(project.name, project.root, {**KEEP_LOGS, "memo": "0"})
    assert legacy.memo == 0 and legacy.memo_cut is None and legacy.memo_rows > 0  # nothing known: nothing goes

    assert build_project(project, force=True).content_hash == report.content_hash  # records the rows it uses
    found = retention.plan(project.name, project.root, {**KEEP_LOGS, "memo": "14d"})
    assert found.memo > 0  # the rows of version 0, unused by the build that recorded, whatever the period
    assert retention.apply(found)["memo"] == found.memo
    warm = build_project(project, force=True)
    assert warm.content_hash == report.content_hash
    assert all(stage["misses"] == 0 for stage in warm.memo.values()), warm.memo
    assert verify_build(project, report.content_hash, cold=True)["match"]

import datetime as dt
import gc
import sqlite3
from pathlib import Path

import pytest

from evo_agents.kg import asked, retention
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
from evo_agents.kg.text import split_passages
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


def test_search_ranks_the_three_ways_together_by_score(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    filler = "Notes on the weekly review of the team. " * 80
    write(app / "docs/tour.md", f"# Tour\n\n{filler}kiwi {filler}rollout {filler}search {filler}outage\n")
    write(app / "docs/kiwi.md", "# Kiwi rollout outage\n\nThe kiwi rollout outage of last week.\n")
    commit(app, "a page holding every word once, and one about them")
    sync_build(project)
    store = Store.for_project(project)
    question = "kiwi rollout search outage"
    assert found(store, question, any_word=False)[0].startswith("app:file:docs/tour.md")  # all words: only there
    hits = found(store, question)
    assert hits[0].startswith("app:file:docs/kiwi.md")  # three of the words, in its title, before all four once
    assert any(h.startswith("app:file:docs/tour.md") for h in hits)  # still found, ranked by its own score


def with_quarry_note(tmp_path, kg_env):
    """The searchable project plus a note with no heading, whose text is its document's body."""
    project = make_searchable(tmp_path, kg_env)
    write(tmp_path / "demo-app" / "docs/quarry.md", "The zephyr quarry closed after the flood of March.\n")
    commit(tmp_path / "demo-app", "a note without a heading")
    sync_build(project)
    return project


def test_a_long_question_is_read_past_its_twelfth_word(tmp_path, kg_env):
    # A question names its subject late: its first twelve words hold nothing the graph has.
    store = Store.for_project(with_quarry_note(tmp_path, kg_env))
    question = " ".join(f"qq{i}" for i in range(14)) + " zephyr quarry"
    assert len(text_search.query_words(question)) == 16
    assert found(store, question)[0] == "app:file:docs/quarry.md"
    assert len(text_search.query_words(" ".join(f"w{i}" for i in range(100)))) == text_search.MAX_TERMS


def test_matched_counts_the_query_words_a_node_holds_and_quotes_the_nodes_asked(tmp_path, kg_env):
    store = Store.for_project(with_quarry_note(tmp_path, kg_env))
    doc, builder = "app:file:docs/quarry.md", "symbol:app:app/hub_build.py::GraphBuilder"
    matched = store.matched([doc, builder, "app:file:nowhere.md"], "zephyr quarry flood tuesday", {doc})
    assert matched[doc]["words"] == 3 and matched[doc]["of"] == 4
    assert "zephyr quarry closed after the flood" in matched[doc]["snippet"]
    assert matched[builder] == {"words": 0, "of": 4}  # held by the index, not quoted: it was not asked for
    assert "app:file:nowhere.md" not in matched  # no row in the index
    assert store.matched([doc], "") == {} and store.matched([], "zephyr") == {}


LONG = "app:file:docs/long.md#long"


def test_split_passages_cuts_at_paragraphs_keeps_a_fence_whole_and_covers_every_line():
    text = "one\ntwo\n\nthree\n\n```\ncode a\n\ncode b\n```\nafter\n"
    whole = split_passages(text, 10, 1_000)
    assert [(p["start"], p["end"]) for p in whole] == [(10, 20)] and whole[0]["text"] == text
    small = split_passages(text, 10, 26)
    assert [(p["start"], p["end"]) for p in small] == [(10, 14), (15, 19), (20, 20)]
    assert "".join(p["text"] for p in small) == text  # every line in one passage, in order
    assert small[1]["text"] == "```\ncode a\n\ncode b\n```\n"  # the blank line inside the fence does not cut it
    long_line = "x" * 50 + "\n"
    cut = split_passages("a\n" + long_line + "b\n", 1, 20)
    assert [(p["start"], p["end"]) for p in cut] == [(1, 1), (2, 2), (3, 3)]  # a block cut between its lines
    assert split_passages("", 1, 20) == [] and split_passages("\n\n", 1, 20) == []


def test_a_long_section_is_searched_by_passages_under_its_own_id(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    b = store.latest_ready()
    keys = [k for (k,) in store.db.execute("SELECT node_id FROM node_fts_rows")]
    passages = sorted(text_search.passage_lines(k)[1] or "" for k in keys if text_search.passage_lines(k)[0] == LONG)
    assert passages == ["", "lines 1-2", "lines 3-3", "lines 4-5"]  # its own row, then its passages
    bodies = [
        body for (body,) in store.db.execute("SELECT body FROM node_fts WHERE node_id = ? ORDER BY rowid", (LONG,))
    ]
    prefixed = [body for body in bodies if body.startswith("docs/long.md > Long\n")]  # its document, its headings
    assert len(bodies) == 4 and len(prefixed) == 3 and NEEDLE in prefixed[-1]
    ids = {nid for (nid,) in store.db.execute("SELECT node_id FROM node_fts")}
    assert ids <= set(store.nodes(ids, b))  # a reader that knows no passage still finds a node for each row
    assert found(store, NEEDLE)[0] == LONG  # one result for the section, not one per passage
    hit = store.matched([LONG], NEEDLE, {LONG})[LONG]
    assert hit["lines"] == "lines 4-5" and NEEDLE in hit["snippet"]  # the passage that holds the word
    plain = store.matched(["app:file:docs/plain.md#ghi-chu"], "duong", set())
    assert "lines" not in plain["app:file:docs/plain.md#ghi-chu"]  # a short section has no passage

    # A graph an older build wrote holds no passage: it is searched and quoted as before.
    store.db.execute(
        "DELETE FROM node_fts WHERE rowid IN (SELECT fts_rowid FROM node_fts_rows WHERE instr(node_id, char(31)))"
    )
    store.db.execute("DELETE FROM node_fts_rows WHERE instr(node_id, char(31))")
    assert found(store, "filler sentence")[0] == LONG
    old = store.matched([LONG], NEEDLE, {LONG})[LONG]
    assert "lines" not in old and old["words"] == 1


def test_the_any_word_query_leaves_out_a_word_the_index_reads_as_a_stop_word():
    # The tokenizer folds accents: "án" is "an" in the index and "cua" is "của", so either would match all of them.
    assert text_search.query_words("Phương án A") == ["Phương", "án"]
    assert list(text_search.fts_queries(["Phương", "án"])) == ['"phương" AND "án"', '"phương"* AND "án"*', '"phương"']
    assert list(text_search.fts_queries(["loi", "cua", "evo"]))[-1] == '"loi" OR "evo"'
    assert list(text_search.fts_queries(["kiểm", "thật"]))[0] == '"kiểm" AND "thật"'  # all words: each one kept
    assert list(text_search.fts_queries(["thật", "án"])) == ['"thật" AND "án"', '"thật"* AND "án"*']
    assert text_search.phrases('("hubsession" OR ("hub" AND "session")) AND "x"* OR "hub"') == [
        '"hubsession"',
        '"hub"',
        '"session"',
        '"x"*',
    ]


def test_a_word_that_reads_as_a_name_and_background_the_query_does_not_ask_for_weigh_a_node():
    assert text_search.named_words("why does Store.search call `_ranked` with HUB_DEGREE and v2?") == [
        "Store.search",
        "_ranked",
        "HUB_DEGREE",
    ]
    assert text_search.named_words("graph builder test") == []  # plain words name nothing
    assert text_search.named_words("GraphBuilder in app/hub_build.py") == ["GraphBuilder", "app/hub_build.py"]
    named = ["Store.search", "hub_build.py"]
    assert text_search.names("symbol:x:evo/kg/store.py::Store.search", "search", "evo/kg/store.py", named)
    assert text_search.names("x:file:app/hub_build.py", "hub_build.py", "app/hub_build.py", named)
    assert not text_search.names("symbol:x:evo/kg/search.py::search", "search", "evo/kg/search.py", named)

    background = text_search.background
    assert background("x:file:tests/kg/test_store.py", "File", "tests/kg/test_store.py") == "test"
    assert background("x:file:tests/fixtures/a.json", "File", "tests/fixtures/a.json") == "fixture"
    assert background("x:file:db/migrations/0001.sql", "File", "db/migrations/0001.sql") == "migration"
    assert background("x:file:web/bundle-budget.json", "File", "web/bundle-budget.json") == "generated"
    assert background("h:file:plans/completed/a.yaml", "Document", "plans/completed/a.yaml") == "closed"
    assert background("h:file:docs/history.md#x", "Section", "docs/history.md") == "history"
    assert background("plan:a/step:2", "PlanStep", "", closed_plan=True) == "closed"
    assert background("x:file:evo/kg/store.py", "File", "evo/kg/store.py") is None
    assert background("plan:a/step:2", "PlanStep", "") is None  # a step of an open plan

    assert text_search.asked("which tests cover the store") == {"test"}
    assert text_search.asked("Kế hoạch đã đóng nào sửa store?") == {"closed"}
    assert text_search.asked("Đóng bước 3: chạy lại store") == set()  # a closing step's title asks for no history
    weight = text_search.weight
    test_file = ("x:file:tests/test_store.py", "File", "test_store.py", "tests/test_store.py")
    assert weight(*test_file, [], set(), False) == text_search.BACKGROUND_WEIGHT
    assert weight(*test_file, [], {"test"}, False) == 1.0
    assert weight(*test_file, ["test_store.py"], {"test"}, False) == text_search.NAMED_WEIGHT


def test_search_ranks_a_test_down_unless_the_query_asks_for_tests_and_a_named_node_up(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "tests/test_zephyr.py", "def test_zephyr_quarry_floods():\n    assert True\n")
    write(app / "app/zephyr.py", "def zephyr_quarry_floods():\n    return 1\n")
    write(app / "app/quarry.py", "def zephyrDrain():\n    return 2\n")
    commit(app, "a test and the code it covers")
    sync_build(project)
    store = Store.for_project(project)
    test_file, code = "app:file:tests/test_zephyr.py", "symbol:app:app/zephyr.py::zephyr_quarry_floods"
    hits = found(store, "zephyr quarry floods")
    assert test_file in hits and hits.index(code) < hits.index(test_file)  # still returned, below the code
    asked, plain = found(store, "zephyr quarry floods test"), found(store, "zephyr quarry floods")
    assert asked.index(test_file) <= plain.index(test_file)  # the query asks for tests: no weight down
    drain = "symbol:app:app/quarry.py::zephyrDrain"
    assert found(store, "how does zephyrDrain empty it")[0] == drain  # a word names it: first


def test_a_file_name_names_the_file_not_its_symbols_and_an_image_is_background():
    named = ["quarry_audit.py", "kg_health_eval"]
    assert text_search.names("app:file:app/quarry_audit.py", "app/quarry_audit.py", "app/quarry_audit.py", named)
    assert text_search.names("x:file:hub/kg_health_eval.py", "hub/kg_health_eval.py", "hub/kg_health_eval.py", named)
    symbol = ("symbol:app:app/quarry_audit.py::LIMIT", "LIMIT", "app/quarry_audit.py")
    assert not text_search.names(*symbol, named)  # a symbol is not named by its file's name
    assert text_search.names(*symbol, ["LIMIT"])

    background = text_search.background
    image = ("h:file:reports/assets/plan-a-change.png", "File", "reports/assets/plan-a-change.png")
    assert background(*image) == "asset"
    assert background("x:file:docs/arch.drawio", "File", "docs/arch.drawio") == "asset"
    assert background("x:file:reports/plan-a.md", "Document", "reports/plan-a.md") == "report"
    assert text_search.weight(*image[:2], "", image[2], [], set(), False) == text_search.ASSET_WEIGHT
    assert text_search.asked("a screenshot of the run page") == {"asset"}
    assert text_search.asked("Sơ đồ kiến trúc của hub") == {"asset"}
    assert text_search.asked("Kiến trúc dịch vụ và mô hình một instance") == set()  # "mô hình" is a model
    assert text_search.asked("ảnh hưởng của cache") == set()


def test_search_finds_a_file_by_its_bare_name_and_weighs_an_image_down(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    code = "".join(f"QUARRY_{n} = {n}\n" for n in ("LIMIT", "DEPTH", "WIDTH", "SLOPE"))
    write(app / "app/quarry_audit.py", code + "\n\ndef audit_quarry():\n    return QUARRY_LIMIT\n")
    image = app / "reports/assets/zephyr-rollout-change.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
    write(app / "docs/zephyr.md", "# Rollout\n\nThe zephyr rollout moves the quarry one change at a time.\n")
    write(app / "app/rollout.py", "def zephyr_rollout(change):\n    return change\n")
    commit(app, "a module of constants, a report's image and a page")
    sync_build(project)
    store = Store.for_project(project)
    file = "app:file:app/quarry_audit.py"
    assert found(store, "quarry_audit.py")[0] == file  # not one of its four constants
    assert found(store, "quarry_audit")[0] == file  # its stem
    assert found(store, "audit_quarry")[0] == "symbol:app:app/quarry_audit.py::audit_quarry"
    png = "app:file:reports/assets/zephyr-rollout-change.png"
    hits = found(store, "zephyr rollout change")  # before ASSET_WEIGHT the image, words in its path only, was first
    assert hits.index("app:file:docs/zephyr.md#rollout") < hits.index(png)
    assert hits.index("symbol:app:app/rollout.py::zephyr_rollout") < hits.index(png)
    assert found(store, "zephyr rollout change screenshot")[0] == png  # the query asks for an image


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


def test_a_build_leaves_no_connection_to_its_store_open(tmp_path, kg_env):
    # Since Python 3.11 a connection left open waits for the cycle collector, and Windows cannot replace a file a
    # connection holds: a rebuild then failed on graph.sqlite-wal (WinError 32).
    project = make_project(tmp_path, kg_env)
    store = (project.root / "graph.sqlite").resolve()
    gc.collect()
    gc.disable()
    try:
        sync_build(project)
        sync_build(project, force=True)
        held = []
        for conn in (o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)):
            try:
                path = conn.execute("PRAGMA database_list").fetchone()[2]
            except sqlite3.ProgrammingError:  # closed
                continue
            if Path(path).resolve() == store:
                held.append(path)
        assert held == []
    finally:
        gc.enable()


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


def fts_rows(store: Store) -> dict[int, str]:
    """rowid -> node id of each row of the full-text index; a node's passages are rows of its own under its id."""
    return dict(store.db.execute("SELECT rowid, node_id FROM node_fts").fetchall())


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
    moved = {nid for rowid, nid in after.items() if before.get(rowid) != nid}
    new_commit = f"app:commit:{git(app, 'rev-parse', 'HEAD').strip()}"  # the commit that added the file
    assert new_commit in moved and all(nid.startswith("app:file:docs/plain.md") for nid in moved - {new_commit})
    keys = store.db.execute("SELECT node_id, fts_rowid FROM node_fts_rows").fetchall()
    assert {rowid: text_search.passage_lines(key)[0] for key, rowid in keys} == after
    assert any(text_search.passage_lines(key)[1] for key, _ in keys)  # docs/long.md is indexed as passages too
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


# -- what agents asked before they read a node (evo_agents.kg.asked) ----------------------------------------------

PLAIN = "app:file:docs/plain.md#ghi-chu"
EVERYTHING = (99, 99)  # a clearance above every label of the demo


def asked_row(node_id: str, words: str, level: int = 0, location: int = 0, audience=None, prior: float = 1.0):
    return asked.Row(node_id, level, location, audience, prior, words)


def test_without_the_asked_index_or_a_clearance_search_ranks_as_before(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    queries = ("graph builder", "do thi tri thuc", "tuesday rollout", "load corpus")
    before = {q: found(store, q) for q in queries}
    store.asked_clearance = EVERYTHING  # a hub reader of a file without the index
    assert not store.has_asked()
    assert {q: found(store, q) for q in queries} == before
    assert store.write_asked([asked_row(PLAIN, "tuesday tuesday rollout")]) is not None
    assert store.has_asked()
    store.asked_clearance = None  # a reader that reads no asked rows (a local session): as before
    assert {q: found(store, q) for q in queries} == before
    assert store.write_asked([]) is None and not store.has_asked()  # no labels: the tables go
    store.asked_clearance = EVERYTHING
    assert {q: found(store, q) for q in queries} == before
    assert store.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == SCHEMA_VERSION


def test_asked_words_find_a_node_only_for_a_reader_their_label_allows(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    assert PLAIN not in found(store, "tuesday rollout")
    store.write_asked(
        [
            asked_row(PLAIN, "tuesday tuesday rollout"),
            asked_row("app:file:docs/do-thi.md#đồ-thị-tri-thức", "zebrafish", level=3),
            asked_row("app:file:app/hub_build.py", "okapi", audience='["chat:7"]'),
        ]
    )
    store.leave_wal()
    store.close()
    store = Store.open_file(project.root / "graph.sqlite", immutable=True)  # as the hub reads a graph it built
    store.asked_clearance = (1, 0)
    assert found(store, "tuesday rollout")[0] == PLAIN  # only the words agents asked find it
    assert found(store, "zebrafish") == []  # a row above the clearance: never read
    assert found(store, "okapi") == []  # a row of an audience the reader is not in
    store.reader = frozenset({"chat:7"})
    assert found(store, "okapi") == ["app:file:app/hub_build.py"]
    assert found(store, "okapi", kinds=["Symbol"]) == []
    store.asked_clearance = (3, 0)
    assert found(store, "zebrafish") == ["app:file:docs/do-thi.md#đồ-thị-tri-thức"]


def test_a_clicked_node_is_weighed_up_by_its_prior_at_most_prior_max(tmp_path, kg_env, monkeypatch):
    project = make_searchable(tmp_path, kg_env)
    sync_build(project)
    store = Store.for_project(project)
    store.asked_clearance = EVERYTHING
    hits = found(store, "graph builder")
    second = hits[1]
    store.write_asked([asked_row(second, "unrelated", prior=9.0)])
    assert store._asked_priors([second, hits[0]]) == {second: asked.PRIOR_MAX}  # stored above it: capped
    monkeypatch.setattr(text_search, "ASKED_WEIGHT", 0.0)  # the words weigh nothing, the prior still counts
    moved = []
    for node in hits[1:8]:
        store.write_asked([asked_row(node, "unrelated", prior=1.0)])
        assert found(store, "graph builder") == hits  # a prior of 1 changes nothing
        store.write_asked([asked_row(node, "unrelated", prior=asked.PRIOR_MAX)])
        now = found(store, "graph builder")
        assert now.index(node) <= hits.index(node) and set(now) == set(hits)
        moved.append(now.index(node) < hits.index(node))
    assert any(moved)
    assert asked.prior(0) == 1.0 and 1.0 < asked.prior(1) < asked.prior(10) <= asked.PRIOR_MAX


class Known(asked.Nodes):
    def __init__(self, ids, paths=None):
        super().__init__(set(ids), paths or {})


NOW = dt.datetime(2026, 10, 28, tzinfo=dt.UTC)


def label(query, clicks=(), results=5, after_grep=(), days=0.0, level=0):
    shown = [{"id": f"n{k}", "rank": k, "path": None} for k in range(1, results + 1)]
    picks = [{"id": f"n{k}", "rank": k, "path": None, "how": "read"} for k in clicks]
    return asked.Label(query, shown, picks, list(after_grep), NOW - dt.timedelta(days=days), level)


def weights(rows) -> dict[str, dict[str, int]]:
    out = {}
    for row in rows:
        counts = {}
        for word in row.asked.split():
            counts[word] = counts.get(word, 0) + 1
        out[row.node_id] = counts
    return out


def test_asked_rows_weigh_clicks_by_propensity_grep_most_and_age_by_half_life():
    nodes = Known({f"n{k}" for k in range(1, 26)} | {"doc"}, {"docs/guide.md": ["doc"]})
    labels = [label("filler words", clicks=[1]) for _ in range(40)]  # the cascade reads rank 1 as always looked at
    labels += [label("deep rank", clicks=[5]), label("top rank", clicks=[1])]
    labels += [label("grep found", after_grep=["/home/x/repo/docs/guide.md"])]
    exam = asked.propensities(labels)
    assert exam[1] == 1.0 and all(exam[k] >= exam[k + 1] >= asked.PROPENSITY_FLOOR for k in range(1, 25))
    got = weights(asked.rows(labels, nodes, NOW))
    assert got["n5"]["deep"] == asked.MAX_REPEAT // 2 + 1  # 1 / floor = 5: the most a click weighs
    assert got["doc"]["grep"] == round(asked.GREP_WEIGHT)  # a file the search missed weighs more than any click
    assert got["doc"]["grep"] > got["n5"]["deep"] > got["n1"]["top"] == 1
    old = asked.rows([label("half life", after_grep=["docs/guide.md"], days=asked.HALF_LIFE_DAYS)], nodes, NOW)
    assert weights(old)["doc"]["half"] == round(asked.GREP_WEIGHT / 2)
    assert old[0].prior < asked.rows([label("half life", after_grep=["docs/guide.md"])], nodes, NOW)[0].prior


def test_asked_rows_keep_each_label_apart_cap_the_words_and_leave_out_holdout_questions():
    nodes = Known({"n1", "n2"})
    many = " ".join(f"word{chr(97 + i)}{chr(97 + j)}" for i in range(5) for j in range(6))  # 30 words
    rows = asked.rows(
        [label(many, clicks=[1]), label("secret plan", clicks=[2], level=2), label("open plan", clicks=[2])],
        nodes,
        NOW,
        holdout=["Where is the hub build of the graph?"],
    )
    assert len(rows[0].asked.split()) == asked.MAX_WORDS
    assert [(r.node_id, r.level, sorted(set(r.asked.split()))) for r in rows[1:]] == [
        ("n2", 0, ["open", "plan"]),
        ("n2", 2, ["plan", "secret"]),
    ]
    kept = asked.rows([label("where is the hub build of the graph", clicks=[1])], nodes, NOW, ["hub build graph?"])
    assert kept == []  # repeats a holdout question: the eval would learn its own answer
    assert asked.rows([label("hub build graph cache", clicks=[1])], nodes, NOW, ["hub build graph"]) == []
    assert asked.rows([label("hub build", clicks=[1])], nodes, NOW, ["hub build graph"]) != []


def test_exploration_sets_the_ratio_of_rank_five_to_four_once_it_has_enough():
    def explored(rank, clicked):
        found = label("q", clicks=[rank] if clicked else [], results=6)
        found.results[rank - 1].update({"explored": rank, "p": 0.01})
        return found

    labels = [explored(4, n < 20) for n in range(60)] + [explored(5, n < 5) for n in range(60)]
    exam = asked.propensities(labels)
    assert exam[5] == pytest.approx(max(exam[4] * 0.25, asked.PROPENSITY_FLOOR))
    few = asked.propensities(labels[:40] + labels[60:100])
    assert few[5] != exam[5] or exam[5] == asked.PROPENSITY_FLOOR

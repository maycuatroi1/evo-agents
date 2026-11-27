"""The graph a build makes of the demo project, read in memory (``tests.kg.built``): the keys of its symbols, its
counts, size and highest label, its full-text search (``evo_agents.kg.search``, ``evo_agents.kg.snapshot_text``) and
the asked index search reads (``evo_agents.kg.asked``). Helpers other test files import live here too."""

import datetime as dt

import pytest

from evo_agents.kg import asked
from evo_agents.kg import search as text_search
from evo_agents.kg.graph_rows import with_asked
from evo_agents.kg.pipeline.code import leading_comment
from evo_agents.kg.project import load_project_at
from evo_agents.kg.snapshot import Snapshot, SnapshotView
from evo_agents.kg.sync import sync_project
from evo_agents.kg.text import split_passages
from tests.kg import built
from tests.kg.hub_graph import hub_session
from tests.kg.projects import commit, make_project, write

APP_SOURCE = "  - id: app\n    connector: git\n    repo: app\n"


def edit_sources(project, old: str, new: str):
    text = project.knowledge_path.read_text(encoding="utf-8")
    assert old in text
    write(project.knowledge_path, text.replace(old, new))
    return load_project_at(project.harness.root, project.home)


def live_keys(project) -> dict[str, list]:
    """node id -> pg_key of each Symbol of the project's latest build."""
    return {n["id"]: n["props"].get("key") for n in built.nodes_of(project) if n["kind"] == "Symbol"}


def test_pg_key_names_every_symbol_by_repo_path_and_qualname(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    built.sync_build(project)
    node = built.view_of(project).node("symbol:app:app/server.py::Server.handle")
    assert node["props"]["key"] == ["app", "app/server.py", "Server.handle"]
    keys = live_keys(project)
    assert keys and all(k is not None for k in keys.values())
    assert len({tuple(k) for k in keys.values()}) == len(keys)


def test_pg_key_duplicate_fails_the_build_and_names_the_sources(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    good = built.sync_build(project)
    # A second source over the same repo: every symbol it reads already has a holder.
    project = edit_sources(project, APP_SOURCE, APP_SOURCE + "  - id: app-copy\n    connector: git\n    repo: app\n")
    assert all(r.ok for r in sync_project(project))
    bad = built.build(project)
    assert not bad.ok
    line = next(e for e in bad.errors if '["app", "app/search.py", "search"]' in e)
    assert line.startswith("duplicate Symbol key")
    assert "symbol:app:app/search.py::search (source app)" in line
    assert "symbol:app-copy:app/search.py::search (source app-copy)" in line
    assert "duplicate Symbol key" in bad.summary_line()
    assert built.snapshot_of(project).build == good.build_id  # the build before stays the latest


def test_pg_key_holder_can_change_between_builds(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    built.sync_build(project)
    before = live_keys(project)

    # Same key, new content.
    text = (app / "app/search.py").read_text(encoding="utf-8")
    write(app / "app/search.py", "# moved down a line\n" + text)
    commit(app)
    edited = built.sync_build(project, verify=True)
    assert edited.verify["match"], edited.verify
    assert built.view_of(project).node("symbol:app:app/search.py::search")["props"]["line"] == 6

    # Same key, new node id: the source is renamed but still reads repo app.
    project = edit_sources(project, APP_SOURCE, APP_SOURCE.replace("id: app", "id: app2"))
    renamed = built.sync_build(project, verify=True, cold=True)
    assert renamed.verify["match"], renamed.verify
    after = live_keys(project)
    assert sorted(after.values()) == sorted(before.values())
    assert all(nid.startswith("symbol:app2:") for nid in after)


def test_kind_counts_split_the_live_nodes_by_kind_and_label(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = built.sync_build(project)
    rows = built.view_of(project).kind_counts()
    assert sum(count for *_, count in rows) == report.nodes
    assert len({tuple(row[:4]) for row in rows}) == len(rows)
    symbols = [row for row in rows if row[0] == "Symbol"]
    assert symbols and all(isinstance(count, int) and count > 0 for *_, count in symbols)


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


def searched(project) -> SnapshotView:
    """Sync and build ``project`` in memory; its latest build as a reader with no audience reads it."""
    built.sync_build(project)
    return built.view_of(project)


def found(view: SnapshotView, query: str, **kw) -> list[str]:
    return view.search(query, **kw)


def test_search_ranks_any_word_when_no_node_holds_them_all(tmp_path, kg_env):
    view = searched(make_searchable(tmp_path, kg_env))
    question = "where does the hub build the knowledge graph on tuesdays"
    assert found(view, question, any_word=False) == []  # no node holds every word: AND alone finds nothing
    hits = found(view, question, limit=5)
    assert hits[0] in ("app:file:app/hub_build.py", "symbol:app:app/hub_build.py::GraphBuilder")
    assert "app:file:app/hub_build.py" in hits[:5]


def test_search_splits_camel_case_reads_d_for_dj_and_indexes_docstrings(tmp_path, kg_env):
    view = searched(make_searchable(tmp_path, kg_env))
    builder, method = (
        "symbol:app:app/hub_build.py::GraphBuilder",
        "symbol:app:app/hub_build.py::GraphBuilder.buildGraph",
    )
    assert found(view, "GraphBuilder")[0] == builder  # its name: through the index, first
    assert builder in found(view, "graph builder")[:3]  # the parts of a camelCase name
    assert method in found(view, "buildgraph")[:3]  # the whole word too
    assert "symbol:app:app/hub_build.py::load_corpus" in found(view, "load corpus")[:3]  # snake_case
    assert "app:file:app/hub_build.py" in found(view, "hub_build")[:3]  # a path
    assert found(view, "corpus into nodes and edges")[0] == builder  # its docstring
    assert "app:file:docs/do-thi.md#đồ-thị-tri-thức" in found(view, "do thi tri thuc")[:3]  # đ reads as d
    assert "app:file:docs/plain.md#ghi-chu" in found(view, "đường ống dữ liệu")[:3]  # both ways
    assert any(h.startswith("app:file:docs/long.md") for h in found(view, NEEDLE))  # past 4,000 characters


def test_search_ranks_the_three_ways_together_by_score(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    filler = "Notes on the weekly review of the team. " * 80
    write(app / "docs/tour.md", f"# Tour\n\n{filler}kiwi {filler}rollout {filler}search {filler}outage\n")
    write(app / "docs/kiwi.md", "# Kiwi rollout outage\n\nThe kiwi rollout outage of last week.\n")
    commit(app, "a page holding every word once, and one about them")
    view = searched(project)
    question = "kiwi rollout search outage"
    assert found(view, question, any_word=False)[0].startswith("app:file:docs/tour.md")  # all words: only there
    hits = found(view, question)
    assert hits[0].startswith("app:file:docs/kiwi.md")  # three of the words, in its title, before all four once
    assert any(h.startswith("app:file:docs/tour.md") for h in hits)  # still found, ranked by its own score


def with_quarry_note(tmp_path, kg_env) -> SnapshotView:
    """The searchable project plus a note with no heading, whose text is its document's body, built."""
    project = make_searchable(tmp_path, kg_env)
    write(tmp_path / "demo-app" / "docs/quarry.md", "The zephyr quarry closed after the flood of March.\n")
    commit(tmp_path / "demo-app", "a note without a heading")
    return searched(project)


def test_a_long_question_is_read_past_its_twelfth_word(tmp_path, kg_env):
    # A question names its subject late: its first twelve words hold nothing the graph has.
    view = with_quarry_note(tmp_path, kg_env)
    question = " ".join(f"qq{i}" for i in range(14)) + " zephyr quarry"
    assert len(text_search.query_words(question)) == 16
    assert found(view, question)[0] == "app:file:docs/quarry.md"
    assert len(text_search.query_words(" ".join(f"w{i}" for i in range(100)))) == text_search.MAX_TERMS


def test_matched_counts_the_query_words_a_node_holds(tmp_path, kg_env):
    view = with_quarry_note(tmp_path, kg_env)
    doc, builder = "app:file:docs/quarry.md", "symbol:app:app/hub_build.py::GraphBuilder"
    matched = view.matched([doc, builder, "app:file:nowhere.md"], "zephyr quarry flood tuesday", {doc})
    assert matched[doc]["words"] == 3 and matched[doc]["of"] == 4
    assert matched[builder] == {"words": 0, "of": 4}  # held by the index, no word of the query
    assert "app:file:nowhere.md" not in matched  # no row in the index
    assert view.matched([doc], "") == {} and view.matched([], "zephyr") == {}


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
    view = searched(project)
    keyed = built.rows_of(project).keyed
    passages = sorted(
        text_search.passage_lines(k)[1] or "" for k, _ in keyed if text_search.passage_lines(k)[0] == LONG
    )
    assert passages == ["", "lines 1-2", "lines 3-3", "lines 4-5"]  # its own row, then its passages
    bodies = [row[4] for k, row in sorted(keyed) if text_search.passage_lines(k)[0] == LONG]
    prefixed = [body for body in bodies if body.startswith("docs/long.md > Long\n")]  # its document, its headings
    assert len(bodies) == 4 and len(prefixed) == 3 and NEEDLE in prefixed[-1]
    ids = {text_search.passage_lines(k)[0] for k, _ in keyed}
    assert ids <= set(view.nodes(ids))  # a reader that knows no passage still finds a node for each row
    assert found(view, NEEDLE)[0] == LONG  # one result for the section, not one per passage
    assert view.matched([LONG], NEEDLE)[LONG]["words"] == 1


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
    view = searched(project)
    test_file, code = "app:file:tests/test_zephyr.py", "symbol:app:app/zephyr.py::zephyr_quarry_floods"
    hits = found(view, "zephyr quarry floods")
    assert test_file in hits and hits.index(code) < hits.index(test_file)  # still returned, below the code
    asked_for, plain = found(view, "zephyr quarry floods test"), found(view, "zephyr quarry floods")
    assert asked_for.index(test_file) <= plain.index(test_file)  # the query asks for tests: no weight down
    drain = "symbol:app:app/quarry.py::zephyrDrain"
    assert found(view, "how does zephyrDrain empty it")[0] == drain  # a word names it: first


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
    view = searched(project)
    file = "app:file:app/quarry_audit.py"
    assert found(view, "quarry_audit.py")[0] == file  # not one of its four constants
    assert found(view, "quarry_audit")[0] == file  # its stem
    assert found(view, "audit_quarry")[0] == "symbol:app:app/quarry_audit.py::audit_quarry"
    png = "app:file:reports/assets/zephyr-rollout-change.png"
    hits = found(view, "zephyr rollout change")  # before ASSET_WEIGHT the image, words in its path only, was first
    assert hits.index("app:file:docs/zephyr.md#rollout") < hits.index(png)
    assert hits.index("symbol:app:app/rollout.py::zephyr_rollout") < hits.index(png)
    assert found(view, "zephyr rollout change screenshot")[0] == png  # the query asks for an image


def test_search_keeps_uris_out_and_filters_kinds_in_the_query(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    view = searched(project)
    assert found(view, "blob") == []  # every node's URI has /blob/main/: none of it is indexed
    sections = found(view, "search", limit=2, kinds=["Section"])
    assert sections and all(":file:docs/" in h and "#" in h for h in sections)
    assert found(view, "rank_results", kinds=["Section"]) == ["app:file:docs/guide.md#details"]
    answer = hub_session(project, "agent").call("kg_search", {"query": "search", "kinds": ["Section"], "limit": 1})
    assert [r["kind"] for r in answer["structuredContent"]["results"]] == ["Section"]


def test_a_text_past_the_cap_is_cut_and_counted_in_coverage(tmp_path, kg_env, monkeypatch):
    monkeypatch.setattr(text_search, "MAX_TEXT_CHARS", 3000)
    project = make_searchable(tmp_path, kg_env)
    report = built.sync_build(project)
    cut = report.coverage["search"]
    assert cut["cut"] == 1 and cut["samples"] == ["app:file:docs/long.md#long"] and cut["max_chars"] == 3000
    assert found(built.view_of(project), NEEDLE) == []


def test_lookups_by_id_name_and_alias(tmp_path, kg_env):
    view = searched(make_searchable(tmp_path, kg_env))
    search = "symbol:app:app/search.py::search"
    assert view.exact(search, 0, 10) == [search]  # by id
    assert view.exact("search", 0, 10, ["Symbol"]) == [search]  # by name, of a kind
    assert view.exact("search", 0, 10, ["Section"]) == []
    assert view.alias_of("usecase:KB-99") is None  # an alias nothing holds
    assert view.node("app:file:app/search.py")["kind"] == "File"


def test_a_rebuild_after_an_edit_searches_the_new_text(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    view = searched(project)
    assert found(view, "zebracorn") == []
    app = tmp_path / "demo-app"
    write(app / "docs/plain.md", "# Ghi chu\n\nDuong ong du lieu chay moi zebracorn.\n")
    commit(app, "plain")
    report = built.sync_build(project, verify=True, cold=True)
    assert report.verify["match"], report.verify
    assert any(nid.startswith("app:file:docs/plain.md") for nid in found(built.view_of(project), "zebracorn"))


def test_search_finds_a_word_inside_a_name_or_a_path_through_trigrams(tmp_path, kg_env):
    assert text_search.trigram_query(["an", "ops", "Đồ", "Dashboard"]) == '"ops" AND "dashboard"'
    assert text_search.trigram_query(["an", "Đồ"]) is None  # no word of three letters: no substring to look up
    project = make_searchable(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/lumenops-dashboard.md", "Panels of the team.\n")
    write(app / "docs/dashboard.md", "# Dashboard\n\nThe dashboard of the dashboard team.\n")
    commit(app, "two dashboards")
    view = searched(project)
    assert found(view, "ops dashboard")[0] == "app:file:docs/lumenops-dashboard.md"  # "ops" is only part of a word


def test_a_build_records_its_size_and_its_highest_label(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    report = built.sync_build(project)
    view = built.view_of(project)
    stats = view.build_row()["stats"]
    assert stats["size"] == {"nodes": report.nodes, "edges": report.edges, "units": report.units}
    assert view.top_label() == (2, 0)  # customer, any location
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


# -- what agents asked before they read a node (evo_agents.kg.asked) ----------------------------------------------

PLAIN = "app:file:docs/plain.md#ghi-chu"
EVERYTHING = (99, 99)  # a clearance above every label of the demo


def asked_row(node_id: str, words: str, level: int = 0, location: int = 0, audience=None, prior: float = 1.0):
    return asked.Row(node_id, level, location, audience, prior, words)


def asked_view(project, found_rows, *, clearance=None, reader=frozenset()) -> SnapshotView:
    """The project's latest build with the asked index ``found_rows``, as the hub keeps it (``with_asked``), read by
    ``reader`` within the asked ``clearance``."""
    rows, _ = with_asked(built.rows_of(project), found_rows, None)
    view = Snapshot.from_stored(rows, 1).view(reader)
    view.asked_clearance = clearance
    return view


def test_without_asked_rows_or_a_clearance_search_ranks_as_before(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    view = searched(project)
    queries = ("graph builder", "do thi tri thuc", "tuesday rollout", "load corpus")
    before = {q: found(view, q) for q in queries}
    view.asked_clearance = EVERYTHING  # a reader of a build without asked rows
    assert {q: found(view, q) for q in queries} == before
    rows = [asked_row(PLAIN, "tuesday tuesday rollout")]
    assert with_asked(built.rows_of(project), rows, None)[1] is not None  # rows: a digest names them
    plain = asked_view(project, rows)  # a reader that reads no asked rows: as before
    assert {q: found(plain, q) for q in queries} == before
    assert with_asked(built.rows_of(project), [], None)[1] is None  # no rows: nothing to name
    empty = asked_view(project, [], clearance=EVERYTHING)
    assert {q: found(empty, q) for q in queries} == before


def test_asked_words_find_a_node_only_for_a_reader_their_label_allows(tmp_path, kg_env):
    project = make_searchable(tmp_path, kg_env)
    assert PLAIN not in found(searched(project), "tuesday rollout")
    rows = [
        asked_row(PLAIN, "tuesday tuesday rollout"),
        asked_row("app:file:docs/do-thi.md#đồ-thị-tri-thức", "zebrafish", level=3),
        asked_row("app:file:app/hub_build.py", "okapi", audience='["chat:7"]'),
    ]
    view = asked_view(project, rows, clearance=(1, 0))
    assert found(view, "tuesday rollout")[0] == PLAIN  # only the words agents asked find it
    assert found(view, "zebrafish") == []  # a row above the clearance: never read
    assert found(view, "okapi") == []  # a row of an audience the reader is not in
    chat = asked_view(project, rows, clearance=(1, 0), reader=frozenset({"chat:7"}))
    assert found(chat, "okapi") == ["app:file:app/hub_build.py"]
    assert found(chat, "okapi", kinds=["Symbol"]) == []
    assert found(asked_view(project, rows, clearance=(3, 0)), "zebrafish") == [
        "app:file:docs/do-thi.md#đồ-thị-tri-thức"
    ]


def test_a_clicked_node_is_weighed_up_by_its_prior_at_most_prior_max(tmp_path, kg_env, monkeypatch):
    project = make_searchable(tmp_path, kg_env)
    built.sync_build(project)
    hits = found(asked_view(project, [], clearance=EVERYTHING), "graph builder")
    second = hits[1]
    capped = asked_view(project, [asked_row(second, "unrelated", prior=9.0)], clearance=EVERYTHING)
    assert capped.ranker._asked_priors([second, hits[0]]) == {second: asked.PRIOR_MAX}  # stored above it: capped
    monkeypatch.setattr(text_search, "ASKED_WEIGHT", 0.0)  # the words weigh nothing, the prior still counts
    moved = []
    for node in hits[1:8]:
        one = asked_view(project, [asked_row(node, "unrelated", prior=1.0)], clearance=EVERYTHING)
        assert found(one, "graph builder") == hits  # a prior of 1 changes nothing
        most = asked_view(project, [asked_row(node, "unrelated", prior=asked.PRIOR_MAX)], clearance=EVERYTHING)
        now = found(most, "graph builder")
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

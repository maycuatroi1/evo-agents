from evo_agents.kg.build import build_project
from evo_agents.kg.pipeline.link import Linker, build_index, looks_like_symbol
from evo_agents.kg.pipeline.stages import map_item
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

INDEX = """# Index

See [the extras](extra/) and [the acme tenant](tenants/acme/), the [app package](/app) and
[a missing tenant](tenants/nobody/).
"""


def sync_build(project, **kw):
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    report = build_project(project, **kw)
    assert report.ok, report.errors
    return report


def live_edges(store: Store, b: int) -> dict[str, tuple]:
    rows = store.db.execute(
        "SELECT edge_id, src, rel, dst, status FROM edges WHERE tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)",
        {"b": b},
    )
    return {r[0]: tuple(r[1:]) for r in rows}


def item_units(store: Store, item_id: str, b: int) -> set[str]:
    rows = store.db.execute(
        "SELECT unit_id FROM units WHERE item_id = :i AND tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)",
        {"i": item_id, "b": b},
    )
    return {r[0] for r in rows}


def tree_project(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/index.md", INDEX)
    write(app / "docs/extra/one.md", "# One\n\nFirst extra.\n")
    write(app / "docs/extra/two.md", "# Two\n\nSecond extra.\n")
    write(app / "docs/tenants/acme/readme.md", "# Acme\n\nA tenant.\n")
    commit(app)
    return project, app


def test_directory_nodes_chain_up_to_the_repo_root(tmp_path, kg_env):
    project, _ = tree_project(tmp_path, kg_env)
    b = sync_build(project).build_id
    store = Store.for_project(project)
    edges = {(s, r, d) for s, r, d, _ in live_edges(store, b).values()}

    acme = store.node("dir:app:docs/tenants/acme", b)
    assert acme["kind"] == "Directory"
    assert acme["name"] == "docs/tenants/acme/"
    assert acme["props"] == {"path": "docs/tenants/acme", "repo": "app"}
    assert ("app:file:docs/tenants/acme/readme.md", "child_of", "dir:app:docs/tenants/acme") in edges
    assert ("dir:app:docs/tenants/acme", "child_of", "dir:app:docs/tenants") in edges
    assert ("dir:app:docs/tenants", "child_of", "dir:app:docs") in edges
    assert ("app:file:app/search.py", "child_of", "dir:app:app") in edges  # a File, not only Documents
    assert not any(s == "dir:app:docs" and r == "child_of" for s, r, _ in edges)  # the root is no node
    assert ("harness:file:plans/active/demo.yaml", "child_of", "dir:demo-harness:plans/active") in edges
    assert ("dir:demo-harness:plans/active", "child_of", "dir:demo-harness:plans") in edges

    # One node per directory however many items sit under it, derived once per item: an OR.
    docs = store.derivations("dir:app:docs", b)
    assert len(docs) == 6
    assert all(len(d["units"]) == 1 and d["units"][0].startswith("app:file:docs/") for d in docs)


def test_directory_mentions_resolve_as_parsed(tmp_path, kg_env):
    project, _ = tree_project(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    mentions = {(s, d): status for s, r, d, status in live_edges(store, report.build_id).values() if r == "mentions"}
    index = "app:file:docs/index.md#index"
    assert mentions[(index, "dir:app:docs/extra")] == "parsed"  # trailing slash, relative to the document
    assert mentions[(index, "dir:app:docs/tenants/acme")] == "parsed"
    assert mentions[(index, "dir:app:app")] == "parsed"  # exact match of a directory from the repo root
    app = next(s for s in report.coverage["sources"] if s["id"] == "app")
    assert "tenants/nobody/" in app["samples"]["dangling"]


def test_directory_path_target_prefers_a_file_and_honours_a_trailing_slash():
    records = [
        {"id": "b:file:tools", "kind": "config", "props": {"path": "tools", "repo": "b"}},
        {"id": "a:file:tools/run.sh", "kind": "code", "props": {"path": "tools/run.sh", "repo": "a"}},
        {"id": "a:file:docs/x.md", "kind": "markdown", "props": {"path": "docs/x.md", "repo": "a"}},
    ]
    index = build_index({r["id"]: map_item(r, r["id"].split(":")[0], True) for r in records}, [])
    assert index["dir"]["tools"] == [{"node": "dir:a:tools", "repo": "a"}]
    assert "tools/run.sh" not in index["dir"]  # a file is never a directory

    outside = Linker(index, "c:file:readme.md", "c", "readme.md")
    assert outside.path_target("tools", None) == ("b:file:tools", "resolved")  # a file wins over a directory
    assert outside.path_target("tools/", None) == ("dir:a:tools", "parsed")  # a trailing slash names a directory
    assert outside.path_target("tools", "a") == ("dir:a:tools", "parsed")
    inside = Linker(index, "a:file:docs/x.md", "a", "docs/x.md")
    assert inside.path_target("../tools", None) == ("dir:a:tools", "parsed")  # its own repo comes first
    assert inside.path_target("tools", None) == ("dir:a:tools", "parsed")
    assert inside.path_target("missing/", None) == (None, "dangling")


def test_directory_nodes_only_for_repo_tree_connectors():
    record = {"id": "wiki:page:a/b", "kind": "page", "props": {"path": "a/b", "repo": "wiki"}}
    assert not [f for f in map_item(record, "wiki") if f.get("kind") == "Directory"]


def test_directory_retracts_with_the_last_item_under_it(tmp_path, kg_env):
    project, app = tree_project(tmp_path, kg_env)
    store = Store.for_project(project)
    b1 = sync_build(project).build_id
    one, two = "app:file:docs/extra/one.md", "app:file:docs/extra/two.md"
    extra = "dir:app:docs/extra"

    # One item left under the directory keeps it alive.
    predicted = store.predict_retraction(item_units(store, one, b1), b1)
    assert extra not in predicted
    (app / "docs/extra/one.md").unlink()
    commit(app)
    second = sync_build(project, verify=True, cold=True)
    assert second.verify["match"], second.verify
    assert second.memo["map"]["misses"] == 1  # the other items' map entries stay valid; the new commit is new
    b2 = second.build_id
    assert store.node(extra, b2) is not None
    assert store.node(one, b2) is None

    # Removing the last one retracts the directory, its own child_of edge and the link to it, nothing more.
    predicted = store.predict_retraction(item_units(store, two, b2), b2)
    assert extra in predicted and "dir:app:docs" not in predicted
    before_nodes, before_edges = store.alive_ids("nodes", b2), live_edges(store, b2)
    (app / "docs/extra/two.md").unlink()
    commit(app)
    third = sync_build(project, verify=True)
    assert third.verify["match"], third.verify
    b3 = third.build_id
    assert store.node(extra, b3) is None
    assert store.node("dir:app:docs", b3) is not None
    assert before_nodes - store.alive_ids("nodes", b3) == {e for e in predicted if e in before_nodes}
    retracted = {before_edges[e][:3] for e in set(before_edges) - store.alive_ids("edges", b3)}
    assert retracted == {before_edges[e][:3] for e in predicted if e in before_edges}
    assert (extra, "child_of", "dir:app:docs") in retracted
    assert ("app:file:docs/index.md#index", "mentions", extra) in retracted


def short_path_index() -> dict:
    paths = {
        "a": ["pkg/__init__.py", "pkg/hub/__init__.py", "pkg/hub/runs.py", "docs/guide/x.md", "docs/guide/notes.md"],
        "b": ["docs/readme.md", "docs/plans/notes.md"],
    }
    records = [
        {"id": f"{r}:file:{p}", "kind": "markdown" if p.endswith(".md") else "code", "props": {"path": p, "repo": r}}
        for r, ps in paths.items()
        for p in ps
    ]
    return build_index({r["id"]: map_item(r, r["id"].split(":")[0], True) for r in records}, [])


def test_a_short_path_resolves_by_the_document_by_package_roots_and_by_a_unique_tail():
    index = short_path_index()
    assert index["roots"] == {"a": ["pkg"]}  # a top-level package: a directory with __init__.py, its parent none
    doc = Linker(index, "a:file:docs/guide/x.md", "a", "docs/guide/x.md")
    other = Linker(index, "b:file:docs/readme.md", "b", "docs/readme.md")
    assert doc.path_target("notes.md", None, guess=True) == ("a:file:docs/guide/notes.md", "parsed")  # its directory
    assert doc.path_target("hub/runs.py", None) == (None, "dangling")  # strict: from the repo root only
    assert doc.path_target("hub/runs.py", None, guess=True) == ("a:file:pkg/hub/runs.py", "resolved")  # a root
    assert other.path_target("hub/runs.py", None, guess=True) == ("a:file:pkg/hub/runs.py", "resolved")  # another's
    assert other.path_target("guide/notes.md", None, guess=True) == ("a:file:docs/guide/notes.md", "resolved")  # tail
    assert doc.path_target("plans/notes.md", None, guess=True) == ("b:file:docs/plans/notes.md", "resolved")


def test_a_path_whose_directory_no_repo_holds_is_outside_the_project():
    doc = Linker(short_path_index(), "a:file:docs/guide/x.md", "a", "docs/guide/x.md")
    assert doc.path_target("vendor/lib/thing.py", None, guess=True) == (None, "outside")
    assert doc.path_target("docs/missing.md", None, guess=True) == (None, "dangling")  # docs/ is the project's
    assert doc.path_target("missing.md", None, guess=True) == (None, "dangling")  # a bare name stays counted


def test_a_plain_lower_case_word_is_not_a_symbol():
    assert not looks_like_symbol("search") and not looks_like_symbol("__init__")
    assert all(looks_like_symbol(n) for n in ("rank_results", "Server", "app.search", "getUser"))


def test_a_short_name_goes_to_the_nearest_symbol_and_stays_ambiguous_on_a_tie():
    index = short_path_index()
    one = {"node": "s:a:pkg/hub/runs.py::Run", "repo": "a", "path": "pkg/hub/runs.py"}
    two = {"node": "s:a:pkg/worker/runs.py::Run", "repo": "a", "path": "pkg/worker/runs.py"}
    far = {"node": "s:b:web/runs.py::Run", "repo": "b", "path": "web/runs.py"}
    assert Linker(index, "a:file:docs/x.md", "a", "docs/x.md").nearest([far, one]) == one["node"]  # its repo
    hub = Linker(index, "a:file:pkg/hub/notes.md", "a", "pkg/hub/notes.md")
    assert hub.nearest([one, two]) == one["node"]  # the most directories shared with the document
    assert Linker(index, "a:file:docs/worker-guide.md", "a", "docs/worker-guide.md").nearest([one, two]) == two["node"]
    commit = Linker(index, "a:commit:1", "a", None)
    commit.touched = frozenset({"pkg/worker/runs.py"})
    assert commit.nearest([one, two]) == two["node"]  # a file the commit changed
    assert Linker(index, "a:file:docs/x.md", "a", "docs/x.md").nearest([one, two]) is None  # still tied


def test_a_url_is_dangling_only_in_a_collection_some_item_is_in():
    records = [
        {"id": "a:file:docs/x.md", "kind": "markdown", "props": {"path": "docs/x.md", "repo": "a"}},
        {"id": "hub:chat:30", "kind": "chat", "props": {"url": "https://hub.test/chats/30"}},
        {"id": "a:pr:1", "kind": "pull_request", "props": {"url": "https://github.com/acme/a/pull/1"}},
    ]
    facts = {r["id"]: map_item(r, r["id"].split(":")[0], True) for r in records}
    index = build_index(facts, [])
    index["url"].update({"https://hub.test/chats/30": "hub:chat:30", "https://github.com/acme/a/pull/1": "a:pr:1"})
    index["url_prefix"] = {"https://hub.test/chats", "https://github.com/acme/a/pull"}
    doc = Linker(index, "a:file:docs/x.md", "a", "docs/x.md")
    doc.scan(
        "a:file:docs/x.md",
        "u",
        "See https://hub.test/chats/30, https://hub.test/chats/31, https://hub.test/v1/health, https://hub.test,"
        " https://github.com/acme/a/pull/2 and https://github.com/acme/a/actions/runs/9.",
    )
    assert doc.stats["resolved"] == 1
    assert doc.samples["dangling"] == ["https://hub.test/chats/31", "https://github.com/acme/a/pull/2"]


def test_the_collection_of_a_url():
    from evo_agents.kg.pipeline.link import _url_prefix

    assert _url_prefix("https://hub.test/chats/30") == "https://hub.test/chats"
    assert _url_prefix("https://hub.test/p/demo/plans/one") == "https://hub.test/p/demo/plans"
    assert _url_prefix("https://hub.test") == "https://hub.test"
    assert _url_prefix("https://github.com/acme/a/pull/1") == "https://github.com/acme/a/pull"
    assert _url_prefix("https://github.com/acme/a/blob/main/docs/x.md") == "https://github.com/acme/a/blob"
    assert _url_prefix("https://git.acme.test/g/a/-/merge_requests/3") == "https://git.acme.test/g/a/-/merge_requests"


def test_a_module_level_upper_case_name_is_a_constant_symbol():
    from evo_agents.kg.extract.python import extract

    found = extract(
        "import os\nMAX_TUNE = 80\n_CACHE: str = 'kg'\nlower = 1\nMAX_TUNE = 90\n\ndef f():\n    X = 1\n", "a/b.py"
    )
    constants = [(s["qualname"], s["line"]) for s in found["symbols"] if s["kind"] == "constant"]
    assert constants == [("MAX_TUNE", 2), ("_CACHE", 3)]  # the first assignment; not a lower-case name or a local


def test_a_name_only_constants_of_several_modules_hold_is_not_ambiguous():
    index = short_path_index()
    one = {"node": "s:a:pkg/hub/runs.py::CACHE", "repo": "a", "path": "pkg/hub/runs.py", "constant": True}
    two = {"node": "s:a:pkg/worker/runs.py::CACHE", "repo": "a", "path": "pkg/worker/runs.py", "constant": True}
    index["symname"]["CACHE"] = [one, two]
    index["symname"]["Run"] = [{**one, "node": "s:a:pkg/hub/runs.py::Run", "constant": False}, {**two, "node": "x"}]
    doc = Linker(index, "a:file:docs/x.md", "a", "docs/x.md")
    doc.scan("a:file:docs/x.md", "u", "The `CACHE` of a module and the `Run` of another.")
    assert doc.stats.get("ambiguous", 0) == 1 and doc.samples.get("ambiguous") == ["Run"]

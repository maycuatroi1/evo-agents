import json
import sqlite3

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import GUIDE, commit, git, make_project, write


def sync_build(project, **kw):
    results = sync_project(project)
    assert all(r.ok for r in results), [r.issues for r in results]
    report = build_project(project, **kw)
    assert report.ok, report.errors
    return report


def edge_keys(store: Store, b: int) -> set[tuple]:
    rows = store.db.execute(
        "SELECT src, rel, dst FROM edges WHERE tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)", {"b": b}
    )
    return {tuple(r) for r in rows}


def test_graph_has_the_expected_links(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    report = sync_build(project)
    store = Store.for_project(project)
    b = report.build_id
    edges = edge_keys(store, b)
    guide = "app:file:docs/guide.md"
    assert (f"{guide}#guide", "mentions", "symbol:app:app/search.py::search") in edges
    assert (f"{guide}#guide", "mentions", "usecase:KB-01") in edges
    assert (f"{guide}#setup", "mentions", "app:file:app/server.py") in edges
    assert (f"{guide}#setup", "mentions", "seam:search-api") in edges
    assert (f"{guide}#usage", "mentions", "plan:demo/step:2") in edges
    assert ("seam:search-api", "owned_by", "repo:app") in edges
    assert ("seam:search-api", "defined_in", "app:file:app/search.py") in edges
    assert ("plan:demo/step:2", "depends_on", "plan:demo/step:1") in edges
    assert ("symbol:app:app/search.py::search", "implements", "usecase:KB-01") in edges
    assert ("symbol:app:app/search.py::search", "calls", "symbol:app:app/search.py::rank_results") in edges
    assert ("app:file:app/server.py", "imports", "app:file:app/search.py") in edges
    assert ("plan:demo/step:1", "mentions", "commit:app@1a2b3c4d") in edges
    declared = store.db.execute("SELECT status FROM edges WHERE rel = 'implements'").fetchone()[0]
    assert declared == "declared"


def test_three_syncs_keep_incremental_equal_to_full_rebuild(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    report = sync_build(project, verify=True)
    assert report.verify["match"]

    write(app / "docs/guide.md", GUIDE.replace("Intro to the app.", "Intro, edited."))  # edit
    commit(app)
    report = sync_build(project, verify=True, cold=True)
    assert report.verify["match"], report.verify

    write(app / "docs/new.md", "# New\n\nMentions app/search.py and KB-03.\n")  # add
    commit(app)
    report = sync_build(project, verify=True)
    assert report.verify["match"], report.verify

    (app / "docs/old.md").unlink()  # delete
    commit(app)
    report = sync_build(project, verify=True, cold=True)
    assert report.verify["match"], report.verify
    store = Store.for_project(project)
    assert store.node("app:file:docs/old.md", report.build_id) is None
    assert store.node("usecase:KB-03", report.build_id) is not None
    assert report.memo["map"]["hits"] > 0  # unchanged items were not recomputed


def test_section_removal_retracts_exactly_what_provenance_predicts(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    first = sync_build(project)
    store = Store.for_project(project)
    b1 = first.build_id
    unit = "app:file:docs/guide.md#setup"
    predicted = store.predict_retraction({unit}, b1)
    before_edges = {
        e: k
        for e, k in store.db.execute(
            "SELECT edge_id, src || ' ' || rel || ' ' || dst FROM edges WHERE tx_from <= :b AND (tx_to IS NULL OR"
            " tx_to > :b)",
            {"b": b1},
        )
    }
    before_nodes = store.alive_ids("nodes", b1)

    # Remove the Setup section's own lines only; its child Details stays and is re-parented.
    without_setup = GUIDE.split("## Setup")[0] + "### Details" + GUIDE.split("### Details")[1]
    write(app / "docs/guide.md", without_setup)
    commit(app)
    second = sync_build(project)
    b2 = second.build_id
    after_edges = store.alive_ids("edges", b2)
    after_nodes = store.alive_ids("nodes", b2)

    retracted_edges = {before_edges[e] for e in set(before_edges) - after_edges}
    predicted_edges = {before_edges[e] for e in predicted if e in before_edges}
    assert retracted_edges == predicted_edges
    assert before_nodes - after_nodes == {e for e in predicted if e in before_nodes}
    assert "app:file:docs/guide.md#setup app:file:docs/guide.md#guide part_of" not in predicted_edges
    assert any("mentions seam:search-api" in e for e in retracted_edges)


def test_unresolved_binding_fails_the_build_and_keeps_the_last_snapshot(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    good = sync_build(project)
    write(
        project.harness.root / "bindings/core.yaml",
        "bindings:\n  - from: app:app/search.py::missing\n    rel: implements\n    to: usecase:KB-01\n",
    )
    sync_project(project)
    bad = build_project(project)
    assert not bad.ok
    assert any("does not resolve" in e for e in bad.errors)
    assert Store.for_project(project).latest_ready() == good.build_id


def test_labels_join_over_inputs(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    report = sync_build(project)
    store = Store.for_project(project)
    b = report.build_id
    plan = store.node("plan:demo", b)
    assert plan["label"][0] == 1  # internal: comes only from the harness
    e = store.db.execute(
        "SELECT label_lvl FROM edges WHERE src = 'plan:demo/step:2' AND rel = 'mentions'"
        " AND dst = 'app:file:app/server.py'"
    ).fetchone()
    assert e[0] == 2  # the edge reveals a customer-level file: it is customer-level too


def audit_builds(project) -> int:
    lines = (project.root / "audit.jsonl").read_text().splitlines()
    return sum(json.loads(line)["event"] == "build" for line in lines)


def test_a_build_of_an_unchanged_corpus_is_skipped_until_something_it_reads_changes(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    first = sync_build(project)
    assert not first.skipped and audit_builds(project) == 1

    again = sync_build(project, verify=True, cold=True)
    assert again.skipped and again.ok and again.verify["match"], again.verify
    assert (again.build_id, again.content_hash) == (first.build_id, first.content_hash)
    assert (again.nodes, again.edges, again.units) == (first.nodes, first.edges, first.units)
    assert again.coverage["sources"] == first.coverage["sources"]
    assert "is current" in again.summary_line() and audit_builds(project) == 1  # nothing was built

    forced = build_project(project, force=True)
    assert not forced.skipped and forced.build_id == first.build_id + 1
    assert forced.content_hash == first.content_hash and audit_builds(project) == 2

    knowledge = project.knowledge_path
    knowledge.write_text(
        knowledge.read_text().replace("      clearance: {level: customer}", "      clearance: {level: public}")
    )
    project = load_project_at(project.harness.root, kg_env)
    config = build_project(project)
    assert not config.skipped and config.build_id == forced.build_id + 1  # the config is an input

    write(tmp_path / "demo-app/docs/guide.md", GUIDE.replace("Intro to the app.", "Intro, edited."))
    commit(tmp_path / "demo-app")
    changed = sync_build(project)
    assert not changed.skipped and changed.content_hash != first.content_hash


def test_link_runs_again_only_for_the_items_whose_reads_changed(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/later.md", "# Later\n\nWill call `fresh_helper` once it exists.\n")
    commit(app)
    first = sync_build(project)
    store = Store.for_project(project)
    assert not any(e[2].endswith("::fresh_helper") for e in edge_keys(store, first.build_id))

    # A body changes, no name does: only that file links again, besides the item of the new commit.
    search = (app / "app/search.py").read_text()
    write(app / "app/search.py", search.replace("return sorted(items)", "return sorted(items, reverse=True)"))
    commit(app)
    body = sync_build(project, verify=True, cold=True)
    assert body.verify["match"], body.verify
    assert body.memo["link"]["misses"] == 2 and body.memo["link"]["hits"] == first.memo["link"]["misses"] - 1

    # A new name: the document that looked it up in vain links again, and now to it.
    write(app / "app/search.py", search + "\n\ndef fresh_helper():\n    return 1\n")
    commit(app)
    named = sync_build(project, verify=True, cold=True)
    assert named.verify["match"], named.verify
    assert named.memo["link"]["misses"] == 3  # app/search.py, docs/later.md and the new commit
    later = ("app:file:docs/later.md#later", "mentions", "symbol:app:app/search.py::fresh_helper")
    assert later in edge_keys(Store.for_project(project), named.build_id)


def test_serve_reads_the_store_read_only_and_compares_labels_only_when_some_are_above_the_sink(
    tmp_path, kg_env, monkeypatch
):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    sync_build(project)
    wide, narrow = Session(project, "wide"), Session(project, "agent")
    store, _ = wide._store()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.db.execute("CREATE TABLE probe (x)")
    assert wide.sees_all  # a customer clearance covers the highest label of the build
    narrow._store()
    assert not narrow.sees_all  # an internal one does not: customer-level files stay hidden

    calls = [
        ("kg_search", {"query": "search"}),
        ("kg_context", {"query": "search rank"}),
        ("kg_node", {"id": "app:file:app/search.py"}),
        ("kg_node", {"id": "plan:demo/step:2"}),
        ("kg_impact", {"paths": ["app/search.py"]}),
    ]

    def answers() -> dict:
        return {sink: [Session(project, sink).call(name, args) for name, args in calls] for sink in ("wide", "agent")}

    skipping = answers()
    monkeypatch.setattr(Store, "top_label", lambda self, b: None)  # every label compared, as before
    assert answers() == skipping
    assert skipping["wide"][2]["structuredContent"]["node"]["id"] == "app:file:app/search.py"
    assert skipping["agent"][2].get("isError")
    assert not any(n["id"].startswith("app:") for n in skipping["agent"][0]["structuredContent"]["results"])


def test_a_done_step_reaches_the_files_its_evidence_commit_changed(tmp_path, kg_env, monkeypatch):
    from evo_agents.kg.connectors import github

    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "app/suggest.py", "def suggest(query):\n    return query\n")  # a file nothing else names
    commit(app, "search: suggest")
    sha = git(app, "rev-parse", "HEAD").strip()
    plan = project.harness.root / "plans/active/demo.yaml"
    plan.write_text(
        plan.read_text().replace(
            "evidence: app@1a2b3c4d", f"evidence: 'app@1a2b3c4d; {sha[:7]} adds suggest, merged through PR #12'"
        )
    )
    pull = {
        "number": 12,
        "title": "Search suggestions",
        "state": "closed",
        "merged_at": "2026-10-01T03:00:00Z",
        "merge_commit_sha": sha,
        "head": {"ref": "feat/suggest"},
        "base": {"ref": "main"},
        "html_url": "https://github.com/acme/demo-app/pull/12",
        "updated_at": "2026-10-01T03:00:00Z",
    }
    monkeypatch.setattr(github, "enabled", lambda env=None: True)
    monkeypatch.setattr(github, "token", lambda env=None: "test-token")
    monkeypatch.setattr(github, "_page", lambda project, page, secret: [pull] if page == 1 else [])
    report = sync_build(project, verify=True, cold=True)
    assert report.verify["match"], report.verify
    store = Store.for_project(project)
    edges = edge_keys(store, report.build_id)
    step, found, pr = "plan:demo/step:1", f"app:commit:{sha}", "app:pr:12"
    assert (step, "mentions", found) in edges  # the bare SHA of the evidence, among the commits of the step's repo
    assert (found, "changes", "app:file:app/suggest.py") in edges
    assert (step, "mentions", pr) in edges and (pr, "merged_as", found) in edges
    assert (step, "mentions", "commit:app@1a2b3c4d") in edges  # a SHA no source read keeps a node of its own
    assert store.node(found, report.build_id)["kind"] == "Commit"

    path = Session(project, "wide").call("kg_path", {"from": step, "to": "app:file:app/suggest.py"})
    data = path["structuredContent"]
    assert data["found"] and data["hops"] == 2
    assert [n["id"] for n in data["nodes"]] == [step, found, "app:file:app/suggest.py"]

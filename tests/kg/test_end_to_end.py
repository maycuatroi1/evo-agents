from evo_agents.kg.build import build_project
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import GUIDE, commit, make_project, write


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

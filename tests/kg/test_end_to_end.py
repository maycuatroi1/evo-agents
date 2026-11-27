import json

from evo_agents.kg.project import load_project_at
from evo_agents.kg.snapshot import SnapshotView
from evo_agents.kg.sync import sync_project
from tests.kg.built import build, edges_of, nodes_of, rows_of, snapshot_of, sync_build
from tests.kg.hub_graph import hub_session
from tests.kg.projects import GUIDE, commit, git, make_project, write


def edge_keys(project) -> set[tuple]:
    """(src, rel, dst) of every edge of the project's latest build."""
    return {(e["src"], e["rel"], e["dst"]) for e in edges_of(project)}


def node_of(project, node_id: str) -> dict | None:
    return next((n for n in nodes_of(project) if n["id"] == node_id), None)


def predict_retraction(rows, removed_units: set[str]) -> set[str]:
    """The provenance circuit of the build's derivations evaluated with ``removed_units`` false: the elements that
    would die."""
    derivs: dict[str, list[tuple[list, list]]] = {}
    for elem, units, needs in rows.derivations:
        derivs.setdefault(elem, []).append((json.loads(units), json.loads(needs)))
    alive = set(derivs)
    changed = True
    while changed:
        changed = False
        for elem in list(alive):
            if not any(not (set(u) & removed_units) and all(n in alive for n in needs) for u, needs in derivs[elem]):
                alive.discard(elem)
                changed = True
    return set(derivs) - alive


def test_graph_has_the_expected_links(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_build(project)
    edges = edge_keys(project)
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
    assert {e["status"] for e in edges_of(project) if e["rel"] == "implements"} == {"declared"}


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
    assert node_of(project, "app:file:docs/old.md") is None
    assert node_of(project, "usecase:KB-03") is not None
    assert report.memo["map"]["hits"] > 0  # unchanged items were not recomputed


def test_section_removal_retracts_exactly_what_provenance_predicts(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    sync_build(project)
    unit = "app:file:docs/guide.md#setup"
    predicted = predict_retraction(rows_of(project), {unit})
    before_edges = {e["id"]: f"{e['src']} {e['rel']} {e['dst']}" for e in edges_of(project)}
    before_nodes = {n["id"] for n in nodes_of(project)}

    # Remove the Setup section's own lines only; its child Details stays and is re-parented.
    without_setup = GUIDE.split("## Setup")[0] + "### Details" + GUIDE.split("### Details")[1]
    write(app / "docs/guide.md", without_setup)
    commit(app)
    sync_build(project)
    after_edges = {e["id"] for e in edges_of(project)}
    after_nodes = {n["id"] for n in nodes_of(project)}

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
    bad = build(project)
    assert not bad.ok
    assert any("does not resolve" in e for e in bad.errors)
    kept = snapshot_of(project)
    assert (kept.build, kept.content_hash) == (good.build_id, good.content_hash)


def test_labels_join_over_inputs(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    sync_build(project)
    plan = node_of(project, "plan:demo")
    assert plan["label"][0] == 1  # internal: comes only from the harness
    key = ("plan:demo/step:2", "mentions", "app:file:app/server.py")
    e = next(e for e in edges_of(project) if (e["src"], e["rel"], e["dst"]) == key)
    assert e["label"][0] == 2  # the edge reveals a customer-level file: it is customer-level too


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

    forced = build(project, force=True)
    assert not forced.skipped and forced.build_id == first.build_id + 1
    assert forced.content_hash == first.content_hash and audit_builds(project) == 2

    knowledge = project.knowledge_path
    knowledge.write_text(
        knowledge.read_text().replace("      clearance: {level: customer}", "      clearance: {level: public}")
    )
    project = load_project_at(project.harness.root, kg_env)
    config = build(project)
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
    assert not any(e[2].endswith("::fresh_helper") for e in edge_keys(project))

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
    assert later in edge_keys(project)


def test_serve_compares_labels_only_when_some_are_above_the_sink(tmp_path, kg_env, monkeypatch):
    project = make_project(tmp_path, kg_env, level="internal", app_level="customer")
    sync_build(project)
    wide, narrow = hub_session(project, "wide"), hub_session(project, "agent")
    wide.store()
    assert wide.visibility.sees_all  # a customer clearance covers the highest label of the build
    narrow.store()
    assert not narrow.visibility.sees_all  # an internal one does not: customer-level files stay hidden

    calls = [
        ("kg_search", {"query": "search"}),
        ("kg_context", {"query": "search rank"}),
        ("kg_node", {"id": "app:file:app/search.py"}),
        ("kg_node", {"id": "plan:demo/step:2"}),
        ("kg_impact", {"paths": ["app/search.py"]}),
    ]

    def answers() -> dict:
        return {
            sink: [hub_session(project, sink).call(name, args) for name, args in calls] for sink in ("wide", "agent")
        }

    skipping = answers()
    monkeypatch.setattr(SnapshotView, "top_label", lambda self, b=0: None)  # every label compared, as before
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
    edges = edge_keys(project)
    step, found, pr = "plan:demo/step:1", f"app:commit:{sha}", "app:pr:12"
    assert (step, "mentions", found) in edges  # the bare SHA of the evidence, among the commits of the step's repo
    assert (found, "changes", "app:file:app/suggest.py") in edges
    assert (step, "mentions", pr) in edges and (pr, "merged_as", found) in edges
    assert (step, "mentions", "commit:app@1a2b3c4d") in edges  # a SHA no source read keeps a node of its own
    assert node_of(project, found)["kind"] == "Commit"

    path = hub_session(project, "wide").call("kg_path", {"from": step, "to": "app:file:app/suggest.py"})
    data = path["structuredContent"]
    assert data["found"] and data["hops"] == 2
    assert [n["id"] for n in data["nodes"]] == [step, found, "app:file:app/suggest.py"]

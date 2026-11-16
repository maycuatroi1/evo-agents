"""The retrieval evaluation of the hub's graph health (``evo_agents.hub.kg_health_eval``) on a step whose evidence
names a commit of the harness: the harness source reads its history, so the step is asked, its answers the harness
files the commit changed."""

import yaml

from evo_agents.hub import kg_health_eval
from tests.kg.built import sync_build, view_of
from tests.kg.hub_graph import hub_session
from tests.kg.projects import make_harness_history_project


def test_a_done_step_naming_a_harness_commit_is_asked_with_the_files_the_commit_changed(tmp_path, kg_env):
    project, sha = make_harness_history_project(tmp_path, kg_env)
    sync_build(project)
    body = yaml.safe_load((project.harness.root / "plans/active/demo.yaml").read_text(encoding="utf-8"))
    commits = kg_health_eval.GraphCommits(view_of(project), 0)
    assert commits.changed(sha[:10], "demo-harness") == (["CLUSTER.md"], 1)
    questions, skipped, available, sources = kg_health_eval.plan_questions("demo", [("demo", body)], commits)
    scored = kg_health_eval.score(questions, skipped, available, hub_session(project, "agent"), sources=sources)
    (question,) = [q for q in questions if q.id == "demo#3"]
    assert question.text == "Map of the cluster" and question.repo == "demo-harness"
    assert question.files == (("demo-harness", "CLUSTER.md"),) and sources == {"commits": 1}
    assert "demo#3" not in {s.step for s in skipped}  # not skipped as not_in_graph
    assert scored["methods"]["kg_search"]["n"] == 1 and scored["methods"]["kg_search"]["errors"] == 0

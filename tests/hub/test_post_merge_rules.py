"""The rules of what follows a merge of the hub's merge queue (``evo_agents.hub.post_merge``; plan gitlab-merge-queue,
criterion a6), as pure functions: a check's next step, the culprit among the merges of a branch, the next deploy to
PROD, the order of a revert's commits, a revert's diff against the merged one, and the figures.
"""

from __future__ import annotations

from types import SimpleNamespace

from evo_agents.hub import judge, post_merge
from evo_agents.hub.post_merge import Candidate, Settled, deploy_target, job_step, pipeline_step, settle


def test_post_merge_a_job_is_played_waited_for_retried_once_then_ends():
    assert job_step("manual", played=False, retried=False) == "play"
    assert job_step("manual", played=True, retried=False) == "wait"  # GitLab has not moved it yet
    assert job_step("running", played=True, retried=False) == "wait"
    assert job_step("success", played=True, retried=False) == "passed"
    assert job_step("failed", played=True, retried=False) == "retry"
    assert job_step("canceled", played=False, retried=False) == "retry"
    assert job_step("failed", played=True, retried=True) == "failed"
    assert job_step("canceled", played=True, retried=True) == "none"
    assert job_step("skipped", played=False, retried=False) == "none"
    assert job_step(None, played=False, retried=False) == "none"  # a declared job missing from the pipeline


def test_post_merge_a_pipeline_as_the_check_passes_green_or_with_its_manual_jobs_left():
    assert pipeline_step(None, retried=False) == "wait"
    assert pipeline_step("running", retried=False) == "wait"
    assert pipeline_step("success", retried=False) == "passed"
    assert pipeline_step("manual", retried=False) == "passed"
    assert pipeline_step("failed", retried=False) == "retry"
    assert pipeline_step("failed", retried=True) == "failed"
    assert pipeline_step("canceled", retried=True) == "none"


def test_post_merge_the_newest_passed_check_clears_the_merges_before_it():
    found = settle([(1, "failed"), (2, None), (3, "passed"), (4, None)])
    assert found == Settled(passed=(3,), cleared=((1, 3), (2, 3)))


def test_post_merge_the_oldest_suspect_whose_own_check_failed_is_the_culprit():
    assert settle([(1, "passed"), (2, "failed"), (3, "failed")]) == Settled(passed=(1,), culprit=2)
    assert settle([(2, "failed"), (3, None)]).culprit == 2  # the newer one waits for the revert's check
    assert settle([(2, None), (3, "failed")]) == Settled()  # the oldest suspect's check still runs: wait


def test_post_merge_an_oldest_suspect_without_a_result_names_the_suspects_and_reverts_none():
    assert settle([(2, "none"), (3, None), (4, "failed"), (5, None)]) == Settled(suspects=(2, 3, 4))
    assert settle([(2, "none")]) == Settled(suspects=(2,))  # its own check broken without a result


def test_post_merge_no_suspect_is_blamed_while_a_revert_has_no_result():
    assert settle([(2, "failed"), (3, "failed")], revert_pending=True) == Settled()
    assert settle([(1, "passed"), (2, "failed")], revert_pending=True) == Settled(passed=(1,))


def test_post_merge_immediate_deploys_in_merge_order_and_carries_what_cannot_go_alone():
    rows = [Candidate(1, False, False, True), Candidate(2, True, False, True), Candidate(3, True, False, True)]
    assert deploy_target(rows, "immediate") == (2, [1])  # !1's own UAT check did not pass: !2's deploy carries it
    behind = [Candidate(2, True, True, True), Candidate(3, True, False, True)]
    assert deploy_target(behind, "immediate") == (3, [2])  # after a culprit: only its revert's deploy carries it
    assert deploy_target([Candidate(2, True, True, True)], "immediate") is None


def test_post_merge_after_uat_and_manual_deploy_the_newest_merge_ready():
    rows = [Candidate(1, True, False, True), Candidate(2, True, False, True), Candidate(3, True, False, False)]
    assert deploy_target(rows, "after_uat") == (2, [1])
    assert deploy_target(rows, "manual") == (2, [1])
    assert deploy_target([Candidate(1, True, False, False)], "manual") is None


def test_post_merge_a_prod_culprit_is_the_deploy_that_carried_no_other_merge():
    assert post_merge.prod_culprit(4, [], "failed") == 4
    assert post_merge.prod_culprit(4, [3], "failed") is None
    assert post_merge.prod_culprit(4, [], "none") is None


def commit(sha: str, *parents: str):
    return SimpleNamespace(id=sha, parent_ids=tuple(parents))


def test_post_merge_a_revert_takes_the_commits_newest_first_whatever_order_gitlab_lists():
    listed = [commit("a1", "base"), commit("c3", "b2"), commit("b2", "a1")]
    assert post_merge.newest_first(listed, "c3") == ["c3", "b2", "a1"]
    assert post_merge.newest_first([commit("x"), commit("y")], "y") == ["y", "x"]


def added(path: str, *lines: str) -> judge.FileDiff:
    return judge.FileDiff(path=path, status="added", added=[(i + 1, line) for i, line in enumerate(lines)])


def removed(path: str, *lines: str) -> judge.FileDiff:
    return judge.FileDiff(
        path=path, old_path=path, status="removed", removed=[(i + 1, line) for i, line in enumerate(lines)]
    )


def test_post_merge_a_revert_must_undo_the_merged_diff_exactly():
    merged = [added("kb/report.py", "def total():", "    return 3")]
    assert post_merge.undo_problem([removed("kb/report.py", "def total():", "    return 3")], merged) is None
    more = [removed("kb/report.py", "def total():", "    return 3"), removed("kb/other.py", "x = 1")]
    assert "not the exact inverse" in post_merge.undo_problem(more, merged)
    assert "no diff" in post_merge.undo_problem([], [])
    binary = [judge.FileDiff(path="kb/logo.png", status="added", readable=False)]
    assert "cannot read" in post_merge.undo_problem(binary, binary)


def test_post_merge_figures_count_by_repo_agent_and_model():
    base = {"repo": "lumen-kb-docs", "model": "claude-sonnet-4-6", "to_uat": None, "to_prod": None}
    rows = [
        {**base, "id": 1, "source": "plan", "profile": "@builder", "state": "done", "judged": True, "waited": 60.0},
        {
            **base,
            "id": 2,
            "source": "plan",
            "profile": "@builder",
            "state": "reverted",
            "judged": True,
            "waited": 120.0,
        },
        {
            **base,
            "id": 3,
            "source": "label",
            "profile": None,
            "model": None,
            "state": "done",
            "judged": True,
            "waited": None,
        },
    ]
    found = post_merge.figures(rows, rolled_back={1}, faulty={3})
    builder = next(item for item in found if item["agent"] == "@builder")
    person = next(item for item in found if item["agent"] == "person")
    assert (builder["merged"], builder["reverted"], builder["rolled_back"], builder["judge_misses"]) == (2, 1, 1, 2)
    assert (builder["wait_seconds"], builder["wait_seconds_max"]) == (90.0, 120.0)
    assert (person["merged"], person["judge_misses"], person["wait_seconds"]) == (1, 1, None)
    assert post_merge.sampled(0.1, 0.05) and not post_merge.sampled(0.1, 0.5) and not post_merge.sampled(0, 0.0)

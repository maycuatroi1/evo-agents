"""The rules of the breaker of each repo of the hub's merge queue (``evo_agents.hub.merge_breaker``; plan
gitlab-merge-queue, criterion a7): what trips it, against the policy's thresholds, and what a trip names. Pure
functions, no database.
"""

from __future__ import annotations

import pytest

from evo_agents.hub import autonomy
from evo_agents.hub import merge_breaker as rules
from evo_agents.hub.merge_breaker import Facts

BREAKER = autonomy.effective(None, {})["breaker"]  # the defaults: 30 minutes, 2 in a row, 2 x baseline, 10%, 10, 2


def test_merge_breaker_reads_the_policys_defaults():
    assert BREAKER == {
        "target_red_minutes": 30,
        "reverts_in_a_row": 2,
        "revert_rate_factor": 2,
        "revert_rate_max": 0.10,
        "baseline_min_merges": 10,
        "judge_misses_7d": 2,
    }


def test_merge_breaker_nothing_trips_it_by_default_and_the_merges_of_a_day_never_do():
    assert rules.causes(Facts(), BREAKER) == []
    busy = Facts(window=(500, 0), new_culprit=True, in_a_row=0)  # five hundred merges, none reverted
    assert rules.causes(busy, BREAKER) == []


def test_merge_breaker_a_target_branch_red_over_its_minutes_trips_it():
    assert rules.causes(Facts(red_minutes=30), BREAKER) == []
    (found,) = rules.causes(Facts(red_minutes=31.4), BREAKER)
    assert found == ("target_red", "its target branch has been red for 31 minutes, over 30")
    assert rules.causes(Facts(red_minutes=31), {**BREAKER, "target_red_minutes": 60}) == []


@pytest.mark.parametrize(
    ("status", "state"),
    [("failed", "red"), ("success", "green"), ("manual", "green"), ("running", None), ("canceled", None), (None, None)],
)
def test_merge_breaker_the_newest_pipeline_of_the_target_branch_turns_it_red_or_green(status, state):
    assert rules.target_state(status) == state


def test_merge_breaker_counts_the_merges_reverted_one_after_the_other():
    culprit, reverted, passed = ("reverting", False, None), ("reverted", False, None), ("done", True, "passed")
    still = ("verifying", False, None)
    assert rules.in_a_row([culprit, passed, reverted]) == 1
    assert rules.in_a_row([passed, culprit, still, reverted]) == 2  # a merge still followed breaks no run
    assert rules.in_a_row([culprit, reverted, passed]) == 0
    assert rules.in_a_row([culprit, ("done", True, "none"), reverted]) == 2  # done without a PROD result: not passed
    assert rules.in_a_row([culprit, ("done", True, None), reverted]) == 1  # no PROD job: passed all its checks
    (found,) = rules.causes(Facts(in_a_row=2), BREAKER)
    assert found == ("reverts_in_a_row", "2 merges in a row were reverted (breaker.reverts_in_a_row)")
    assert rules.causes(Facts(in_a_row=2), {**BREAKER, "reverts_in_a_row": 3}) == []


def test_merge_breaker_the_revert_rate_against_the_absolute_threshold_before_a_baseline():
    assert rules.rate_problem((10, 1), None, factor=2, most=0.10, min_merges=10) is None  # 10% is not over 10%
    assert rules.rate_problem((9, 1), None, factor=2, most=0.10, min_merges=10) == (
        "1 of its 9 merges of the last 14 days were reverted (11%), over breaker.revert_rate_max 10%, the repo having "
        "no baseline yet"
    )
    # first days with too few merges for a baseline: the absolute threshold still
    assert rules.rate_problem((9, 1), (9, 0), factor=2, most=0.10, min_merges=10) is not None
    assert rules.rate_problem((0, 0), None, factor=2, most=0.10, min_merges=10) is None


def test_merge_breaker_the_revert_rate_against_its_baseline_once_the_first_days_are_over():
    baseline = (20, 2)  # 10% in the first 14 days
    assert rules.rate_problem((10, 2), baseline, factor=2, most=0.10, min_merges=10) is None  # 20% is 2 x 10%
    assert rules.rate_problem((9, 2), baseline, factor=2, most=0.10, min_merges=10) == (
        "2 of its 9 merges of the last 14 days were reverted (22%), over breaker.revert_rate_factor 2 times the "
        "repo's baseline 10%"
    )
    assert rules.rate_problem((10, 1), (20, 0), factor=2, most=0.10, min_merges=10) is not None  # a baseline of 0


def test_merge_breaker_the_rate_and_the_judges_misses_trip_it_only_for_one_found_since_the_last_reset():
    over = Facts(window=(4, 2))
    assert rules.causes(over, BREAKER) == []
    assert [cause for cause, _ in rules.causes(Facts(window=(4, 2), new_culprit=True), BREAKER)] == ["revert_rate"]
    assert rules.causes(Facts(misses=3), BREAKER) == []
    assert rules.causes(Facts(misses=2, new_miss=True), BREAKER) == []  # 2 allowed, the third trips it
    (found,) = rules.causes(Facts(misses=3, new_miss=True), BREAKER)
    assert found == ("judge_misses", "the Judge passed 3 merges found wrong in 7 days, over 2")


def test_merge_breaker_each_failure_names_its_cause_and_its_merge_request():
    assert rules.failure_of("uat_check", "failed", 3, "job uat-smoke ended failed", revert=False) == (
        "uat_check",
        "the UAT check of !3 failed: job uat-smoke ended failed",
    )
    assert rules.failure_of("prod_check", "none", 4, None, revert=False) == (
        "prod_check",
        "the PROD check of !4 has no result",
    )
    assert rules.failure_of("prod_deploy", "failed", 4, None, revert=False)[1] == "the PROD deploy of !4 failed"
    assert rules.failure_of("rollback", None, 4, "rollback-prod ended success", revert=False) == (
        "rollback",
        "the hub rolled PROD back for !4: rollback-prod ended success",
    )
    assert rules.failure_of("revert_opened", None, 5, "conflict", revert=False) == (
        "revert",
        "GitLab would not revert !5: conflict",
    )
    assert rules.failure_of("gate", None, 6, "gate 1 (pipeline): red", revert=True) == (
        "revert",
        "the revert !6 is blocked: gate 1 (pipeline): red",
    )


def test_merge_breaker_its_causes_go_in_order_and_its_reason_names_each_once():
    failures = (
        ("rollback", "the hub rolled PROD back for !4"),
        ("prod_check", "the PROD check of !4 failed"),
        ("weather", "never a cause"),
    )
    found = rules.causes(Facts(red_minutes=45, failures=failures), BREAKER)
    assert [cause for cause, _ in found] == ["prod_check", "rollback", "target_red"]  # what happened first
    assert rules.reason_of([*found, found[1]]) == (
        "the PROD check of !4 failed; the hub rolled PROD back for !4; its target branch has been red for 45 "
        "minutes, over 30"
    )
    long = rules.reason_of([("uat_check", "x" * 3000)])
    assert len(long) == rules.MAX_REASON_CHARS and long.endswith("...")
    assert set(rules.CAUSE_WORDS) == set(rules.CAUSES)

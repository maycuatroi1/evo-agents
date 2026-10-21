"""What follows a merge of the hub's merge queue on GitLab (``evo_agents.hub.server.post_merge``; plan
gitlab-merge-queue, criterion a6), against Postgres and the fake GitLab of ``tests.hub.fake_gitlab``: the real GitLab is
never called.

The checks step 6 names: the hub follows the pipeline of each merge's commit on the target branch and the jobs the
policy names, plays a manual job and runs a failed one again once; a repo that names no job stops at its merge with no
check after deploy; PROD goes immediate, after_uat or manual, in merge order, one deploy after the PROD check of the one
before; a check that fails again finds its culprit from the last good commit (with hold_until merged, the oldest
suspect decides; a suspect without a result reverts none and asks an admin); PROD is rolled back before the revert
opens, and the revert goes first in the queue, gated by its diff undoing the merged one. The world is
``tests.hub.test_merge_gate``'s; the projects and people are fictional.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_merge_gate import (  # noqa: F401 (world: a fixture)
    CLEAN,
    PROJECT,
    REPO,
    gate_pass,
    green,
    put_policy,
    world,
)
from tests.hub.test_merge_queue import by_iid, judge_next, kinds, labeled

JOBS = {"uat_check": "uat-smoke", "prod_deploy": "deploy-prod", "prod_check": "prod-smoke", "rollback": "rollback-prod"}
MANUAL = [(name, "manual") for name in JOBS.values()]
UNDONE = [  # the diff of the revert of CLEAN
    {
        "old_path": item["new_path"],
        "new_path": item["new_path"],
        "diff": item["diff"].replace("@@ -0,0 +1,2 @@", "@@ -1,2 +0,0 @@").replace("\n+", "\n-"),
        "new_file": False,
        "renamed_file": False,
        "deleted_file": True,
    }
    for item in CLEAN
]


def policy(world, *, jobs=None, release: str = "immediate", hold: str = "merged", **repo) -> None:
    body = {
        "repos": {REPO: {"mode": "auto", "target_branch": "dev", "prod_release": release, "jobs": jobs or {}, **repo}},
        "judge": {"worker": "mac-mini"},
        "merge": {"hold_until": hold},
    }
    put_policy(world.client, world.headers["owner"], body)


def merged(world, branch: str, title: str):
    """A person's merge request, labeled and green, through its gates and its turn until the queue merged it."""
    mr = labeled(world, branch, title)
    gate_pass(world)
    judge_next(world)
    gate_pass(world)
    if mr.state != "merged":  # dev moved: its turn rebased it, and its pipeline runs at the new head
        green(world, mr)
        gate_pass(world)
    assert mr.state == "merged", mr
    return mr


def on_dev(world, mr, jobs=MANUAL, status: str = "running") -> dict:
    """The pipeline of ``mr``'s commit on dev, with ``jobs``."""
    return world.fake.add_pipeline(world.project, mr.sha, "dev", status, jobs=jobs)


def job(world, pipeline: dict, name: str) -> dict:
    """The last try of job ``name`` of ``pipeline``."""
    return max((item for item in world.project.jobs[pipeline["id"]] if item["name"] == name), key=lambda i: i["id"])


def ends(world, pipeline: dict, name: str, status: str) -> None:
    job(world, pipeline, name)["status"] = status


def played(world) -> list[str]:
    """The name of each job the hub played, in order, with the commit PROD went back to for a rollback."""
    names = {item["id"]: item["name"] for jobs in world.project.jobs.values() for item in jobs}
    return [names[job_id] + (f"@{v['EVO_ROLLBACK_TO_SHA'][:7]}" if v else "") for job_id, v in world.project.played]


def lines(entry: dict, kind: str) -> list[dict]:
    """The ledger lines of ``kind`` of ``entry``, the oldest first."""
    return [event for event in reversed(entry["events"]) if event["kind"] == kind]


def notices(world) -> list[dict]:
    response = world.client.get("/v1/me/notifications", headers=world.headers["owner"])
    assert response.status_code == 200, response.text
    return [item for item in response.json()["notifications"] if item["notice_kind"] == "merge_default_branch"]


# A repo that names no job


def test_post_merge_a_repo_without_jobs_stops_at_its_merge_once_its_pipeline_on_dev_is_green(world):
    policy(world, hold="uat_check")
    first = merged(world, "fix/one", "Fix one")
    entry = by_iid(world)[1]
    assert entry["state"] == "merged" and entry["after_sha"] == first.sha
    second = labeled(world, "fix/two", "Fix two")
    gate_pass(world)
    judge_next(world)
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["reason"] == f"after its merge, it waits: no pipeline of {first.sha[:12]} on dev yet"
    assert entries[2]["state"] == "ready" and "waits for !1" in entries[2]["reason"]  # merge.hold_until uat_check

    pipeline = on_dev(world, first, jobs=())
    gate_pass(world)
    assert by_iid(world)[1]["state"] == "verifying"
    pipeline["status"] = "failed"  # a flake: the hub runs it again once
    gate_pass(world)
    assert world.project.retried == [pipeline["id"]] and by_iid(world)[1]["state"] == "verifying"
    pipeline["status"] = "success"
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == "done" and entries[1]["uat_result"] == "passed"
    (check,) = lines(entries[1], "uat_check")
    assert check["passed"] is True and check["reason"].endswith("the repo names no job: no check after deploy")
    assert lines(entries[1], "retried")[0]["detail"]["pipeline"] == pipeline["id"]
    assert entries[2]["state"] == "merged" and second.state == "merged"  # !1 passed its check: the turn of !2 came
    assert world.project.played == [] and notices(world) == []


# The UAT check and PROD, immediate


def test_post_merge_plays_the_uat_check_then_deploys_to_prod_in_merge_order(world):
    policy(world, jobs=JOBS)
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one, two = on_dev(world, first), on_dev(world, second)
    gate_pass(world)
    assert played(world) == ["uat-smoke", "uat-smoke"]
    assert {entry["state"] for entry in by_iid(world).values()} == {"verifying"}

    ends(world, one, "uat-smoke", "success")
    ends(world, two, "uat-smoke", "success")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == entries[2]["state"] == "releasing"
    assert played(world)[2:] == ["deploy-prod"] and world.project.played[2][0] == job(world, one, "deploy-prod")["id"]
    (deploy,) = lines(entries[1], "prod_deploy")
    assert deploy["reason"] == f"the hub runs job deploy-prod of {first.sha[:12]}"

    ends(world, one, "deploy-prod", "success")
    gate_pass(world)
    assert played(world)[3:] == ["prod-smoke"]  # the PROD check of !1, and no deploy of !2 before it ends
    gate_pass(world)
    assert played(world)[3:] == ["prod-smoke"]
    ends(world, one, "prod-smoke", "success")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == "done" and entries[1]["prod_result"] == "passed"
    gate_pass(world)
    assert played(world)[4:] == ["deploy-prod"] and world.project.played[4][0] == job(world, two, "deploy-prod")["id"]
    ends(world, two, "deploy-prod", "success")
    gate_pass(world)
    ends(world, two, "prod-smoke", "success")
    gate_pass(world)
    entry = by_iid(world)[2]
    assert entry["state"] == "done" and entry["prod_result"] == "passed"
    assert kinds(entry)[kinds(entry).index("merged") :] == [
        "merged",
        "uat_check",  # played
        "uat_check",  # passed
        "prod_deploy",  # played
        "prod_deploy",  # passed
        "prod_check",  # played
        "prod_check",  # passed
    ]
    a = tables.audit
    deploys = [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == "merge.deploy").order_by(a.c.id))]
    assert deploys == [
        f"{PROJECT}/{REPO} !1 job=deploy-prod sha={first.sha}",
        f"{PROJECT}/{REPO} !2 job=deploy-prod sha={second.sha}",
    ]


def test_post_merge_a_flaky_uat_check_passes_on_its_one_retry(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first = merged(world, "fix/one", "Fix one")
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "running")])
    gate_pass(world)
    ends(world, pipeline, "uat-smoke", "failed")
    gate_pass(world)
    entry = by_iid(world)[1]
    assert entry["state"] == "verifying" and entry["reason"].endswith("the hub runs it again once")
    assert job(world, pipeline, "uat-smoke")["status"] == "pending"  # its retry
    ends(world, pipeline, "uat-smoke", "success")
    gate_pass(world)
    entry = by_iid(world)[1]
    assert entry["state"] == "done" and entry["uat_result"] == "passed"
    assert [line["passed"] for line in lines(entry, "uat_check")] == [True]
    assert lines(entry, "uat_check")[0]["reason"].endswith("ended success on its retry")


# The culprit and its revert


def fail_twice(world, pipeline: dict, name: str) -> None:
    """Job ``name`` fails, then fails again after the hub's retry."""
    ends(world, pipeline, name, "failed")
    gate_pass(world)
    ends(world, pipeline, name, "failed")


def test_post_merge_a_uat_check_that_fails_again_reverts_its_merge_first_in_the_queue(world):
    policy(world, jobs={"uat_check": "uat-smoke"}, hold="uat_check")
    first = merged(world, "fix/one", "Fix one")
    labeled(world, "fix/two", "Fix two")
    gate_pass(world)
    judge_next(world)
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "manual")])
    gate_pass(world)
    fail_twice(world, pipeline, "uat-smoke")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == "reverting" and entries[1]["uat_result"] == "failed"
    assert entries[1]["reason"].startswith(f"the culprit: its UAT check on its own commit {first.sha[:12]} failed")
    assert world.project.reverted == [(first.sha, "evo-revert/1")]
    revert = world.project.merge_requests[3]
    assert revert.source_branch == "evo-revert/1" and revert.title == "Revert !1: Fix one"
    assert entries[3]["source"] == "revert" and entries[3]["reverts_id"] == entries[1]["id"]
    (opened,) = lines(entries[1], "revert_opened")
    assert opened["passed"] is True and opened["detail"] == {"iid": 3, "branch": "evo-revert/1", "commits": [first.sha]}
    assert entries[2]["state"] == "ready"  # held by !1, which failed its check

    green(world, revert, UNDONE)
    assert gate_pass(world) == {"merged": 1, "ready": 1}  # no Judge: its diff undoes !1's; first in line, never held
    entries = by_iid(world)
    assert entries[3]["state"] == "merged" and entries[1]["state"] == "reverted"
    assert lines(entries[1], "reverted")[0]["reason"].startswith("its revert !3 merged into dev")
    gates = [(line["gate"], line["passed"]) for line in lines(entries[3], "gate")]
    assert gates[:4] == [(0, True), (1, True), (2, True), (3, True)] and "judged" not in kinds(entries[3])
    assert entries[2]["state"] == "ready" and "waits for !3" in entries[2]["reason"]  # the revert's own check now

    on_dev(world, revert, jobs=[("uat-smoke", "success")])
    gate_pass(world)
    entries = by_iid(world)
    assert entries[3]["state"] == "done" and entries[2]["state"] == "testing"
    a = tables.audit
    target = sql(world.db, select(a.c.target).where(a.c.action == "merge.revert"))
    assert target == [(f"{PROJECT}/{REPO} !1 revert=!3 branch=evo-revert/1",)]


def test_post_merge_a_revert_whose_diff_does_not_undo_the_merge_is_blocked(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first = merged(world, "fix/one", "Fix one")
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "running")])
    gate_pass(world)
    fail_twice(world, pipeline, "uat-smoke")
    gate_pass(world)
    green(world, world.project.merge_requests[2], CLEAN)  # it adds the lines again instead
    gate_pass(world)
    entry = by_iid(world)[2]
    assert entry["state"] == "blocked"
    assert entry["reason"] == (
        "gate 3 (score hacking): a revert: its diff is not the exact inverse of the diff merged: a person checks it"
    )


def test_post_merge_with_hold_until_merged_the_oldest_suspect_that_failed_is_the_culprit(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one, two = (
        on_dev(world, first, jobs=[("uat-smoke", "running")]),
        on_dev(world, second, jobs=[("uat-smoke", "running")]),
    )
    gate_pass(world)
    fail_twice(world, two, "uat-smoke")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[2]["uat_result"] == "failed" and entries[1]["state"] == "verifying"  # !1's check still runs: wait
    assert world.project.reverted == []
    fail_twice(world, one, "uat-smoke")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == "reverting" and entries[2]["state"] == "verifying"
    assert world.project.reverted == [(first.sha, "evo-revert/1")]  # !2 waits for the check of !1's revert
    q = tables.merge_queue
    behind = sql(world.db, select(q.c.follow["behind"].as_integer()).where(q.c.iid == 2))
    assert behind == [(entries[1]["id"],)]


def test_post_merge_an_oldest_suspect_without_a_result_reverts_none_and_asks_the_admins(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one, two = (
        on_dev(world, first, jobs=[("uat-smoke", "running")]),
        on_dev(world, second, jobs=[("uat-smoke", "running")]),
    )
    gate_pass(world)
    ends(world, one, "uat-smoke", "canceled")
    fail_twice(world, two, "uat-smoke")
    ends(world, one, "uat-smoke", "canceled")  # its retry, cancelled too: no result
    gate_pass(world)
    entries = by_iid(world)
    assert (entries[1]["uat_result"], entries[2]["uat_result"]) == ("none", "failed")
    assert world.project.reverted == [] and {entries[1]["state"], entries[2]["state"]} == {"verifying"}
    assert entries[1]["reason"].startswith("its UAT check failed and the hub cannot tell which of !1, !2 broke it")
    (notice,) = notices(world)
    assert notice["title"] == f"The UAT check of {REPO} failed: which of !1, !2 broke dev?"
    assert notice["details"]["ask"] == "revert" and notice["link"] == f"/p/{PROJECT}/merge-queue"
    assert [item["iid"] for item in notice["details"]["suspects"]] == [1, 2]
    assert notice["details"]["commits"] == [second.sha] and notice["details"]["branch"] == "dev"
    gate_pass(world)
    assert len(notices(world)) == 1  # asked once

    url = f"/v1/projects/{PROJECT}/merge-queue/{entries[2]['id']}/revert"
    assert world.client.post(url, headers=world.headers["other"]).status_code == 403  # a writer
    answer = world.client.post(url, headers=world.headers["owner"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "reverting"
    gate_pass(world)
    assert world.project.reverted == [(second.sha, "evo-revert/2")]
    again = world.client.post(
        f"/v1/projects/{PROJECT}/merge-queue/{entries[2]['id']}/revert", headers=world.headers["owner"]
    )
    assert again.status_code == 409


# PROD failing


def test_post_merge_a_prod_check_that_fails_again_rolls_prod_back_before_the_revert(world):
    policy(world, jobs=JOBS)
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one, two = on_dev(world, first), on_dev(world, second)
    gate_pass(world)
    for pipeline in (one, two):
        ends(world, pipeline, "uat-smoke", "success")
    gate_pass(world)  # !1 deploys
    ends(world, one, "deploy-prod", "success")
    gate_pass(world)
    ends(world, one, "prod-smoke", "success")
    gate_pass(world)  # !1 is in PROD and passed its check
    gate_pass(world)  # !2 deploys
    ends(world, two, "deploy-prod", "success")
    gate_pass(world)
    fail_twice(world, two, "prod-smoke")
    gate_pass(world)
    entry = by_iid(world)[2]
    assert entry["state"] == "rolling_back" and entry["prod_result"] == "failed"
    assert played(world)[-1] == "prod-smoke" and world.project.reverted == []
    gate_pass(world)
    assert played(world)[-1] == f"rollback-prod@{first.sha[:7]}"  # back to the last commit whose PROD check passed
    assert world.project.reverted == []  # the rollback first
    ends(world, two, "rollback-prod", "success")
    gate_pass(world)
    entry = by_iid(world)[2]
    assert entry["state"] == "reverting" and world.project.reverted == [(second.sha, "evo-revert/2")]
    rollback = lines(entry, "rollback")
    assert [line["passed"] for line in rollback] == [None, True] and rollback[-1]["sha"] == first.sha
    a = tables.audit
    targets = sql(world.db, select(a.c.target).where(a.c.action == "merge.rollback"))
    assert targets == [(f"{PROJECT}/{REPO} !2 job=rollback-prod to={first.sha}",)]


# PROD after UAT, and by hand


def test_post_merge_after_uat_deploys_the_newest_merge_its_hours_after_carrying_those_before(world):
    policy(world, jobs={**JOBS, "prod_check": None}, release="after_uat", after_uat_hours=1)
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one, two = on_dev(world, first), on_dev(world, second)
    gate_pass(world)
    ends(world, one, "uat-smoke", "success")
    ends(world, two, "uat-smoke", "success")
    gate_pass(world)
    gate_pass(world)
    assert "deploy-prod" not in played(world)  # an hour has not passed
    q = tables.merge_queue
    sql(world.db, update(q).values(uat_at=func.now() - timedelta(hours=2)))
    gate_pass(world)
    assert world.project.played[-1][0] == job(world, two, "deploy-prod")["id"]  # !2's deploy, carrying !1
    assert by_iid(world)[1]["reason"] == "its PROD deploy rides with !2's"
    ends(world, two, "deploy-prod", "success")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == entries[2]["state"] == "done"
    assert lines(entries[1], "prod_check")[0]["reason"] == "carried by the deploy of !2, whose PROD check passed"
    assert lines(entries[2], "prod_check")[0]["reason"] == "its deploy passed, and the repo names no PROD check job"


def test_post_merge_manual_asks_the_admins_and_deploys_once_one_approves(world):
    policy(world, jobs={"uat_check": "uat-smoke", "prod_deploy": "deploy-prod"}, release="manual")
    first = merged(world, "fix/one", "Fix one")
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "success"), ("deploy-prod", "manual")])
    gate_pass(world)
    gate_pass(world)
    entry = by_iid(world)[1]
    assert entry["state"] == "releasing" and "deploy-prod" not in played(world)
    assert entry["reason"] == "its PROD release waits for an admin to approve it (prod_release manual)"
    (notice,) = notices(world)
    assert notice["details"]["ask"] == "release" and notice["details"]["queue_id"] == entry["id"]
    gate_pass(world)
    assert len(notices(world)) == 1

    url = f"/v1/projects/{PROJECT}/merge-queue/{entry['id']}/release"
    assert world.client.post(url, headers=world.headers["other"]).status_code == 403
    assert world.client.post(url, headers=world.headers["owner"]).status_code == 200
    gate_pass(world)
    assert played(world) == ["deploy-prod"]
    ends(world, pipeline, "deploy-prod", "success")
    gate_pass(world)
    assert by_iid(world)[1]["state"] == "done"
    a = tables.audit
    approved = sql(world.db, select(a.c.target).where(a.c.action == "merge.release"))
    assert approved == [(f"{PROJECT}/{REPO} !1 approved",)]
    assert world.client.post(url, headers=world.headers["owner"]).status_code == 409  # released already


# The figures


def test_post_merge_figures_count_merges_reverts_and_rollbacks_by_repo_agent_and_model(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    on_dev(world, first, jobs=[("uat-smoke", "success")])
    two = on_dev(world, second, jobs=[("uat-smoke", "running")])
    gate_pass(world)
    fail_twice(world, two, "uat-smoke")
    gate_pass(world)
    response = world.client.get(f"/v1/projects/{PROJECT}/merge-queue/figures", headers=world.headers["other"])
    assert response.status_code == 200, response.text
    (figure,) = response.json()["figures"]
    assert (figure["repo"], figure["agent"], figure["model"]) == (REPO, "person", None)
    assert (figure["merged"], figure["reverted"], figure["rolled_back"], figure["judge_misses"]) == (2, 1, 0, 1)
    assert figure["wait_seconds"] is not None and figure["uat_seconds"] is not None

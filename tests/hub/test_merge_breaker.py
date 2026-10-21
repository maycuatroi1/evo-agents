"""The breaker of each repo of the hub's merge queue on GitLab (``evo_agents.hub.server.merge_breaker``; plan
gitlab-merge-queue, criterion a7), against Postgres and the fake GitLab of ``tests.hub.fake_gitlab``: the real GitLab
is never called.

The checks step 7 names: the breaker of a repo trips on a UAT or PROD check that failed or has no result, a rollback,
a revert GitLab will not make, a target branch red too long, a revert rate over its baseline (or over the absolute
threshold before one), and too many passes of the Judge found wrong; never on the number of merges. Tripped, it stops
the merges and the deploys of that repo (a merge request ready or in its turn is ``paused``, one at its gates waits, a
deploy job is not run again), while the rollback and the revert of the failure run to their end; the admins hear of
it in the Inbox (notice merge_paused); only an admin resets it, over the route or ``evo-agents hub merge resume``. The
world is ``tests.hub.test_merge_gate``'s; the projects and people are fictional.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_merge_gate import PROJECT, REPO, gate_pass, green, put_policy, world  # noqa: F401 (world)
from tests.hub.test_merge_queue import by_iid, judge_next, judge_runs, kinds, labeled
from tests.hub.test_post_merge import JOBS, UNDONE, ends, fail_twice, job, merged, on_dev, played, policy
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

RESUME = f"/v1/projects/{PROJECT}/merge-queue/repos/{REPO}/resume"


def view(world, who: str = "owner") -> dict:
    response = world.client.get(f"/v1/projects/{PROJECT}/merge-queue", headers=world.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def breaker(world) -> dict:
    (found,) = [item for item in view(world)["breakers"] if item["repo"] == REPO]
    return found


def resume(world, who: str = "owner"):
    return world.client.post(RESUME, headers=world.headers[who])


def paused_notices(world, who: str = "owner") -> list[dict]:
    response = world.client.get("/v1/me/notifications", headers=world.headers[who])
    assert response.status_code == 200, response.text
    return [item for item in response.json()["notifications"] if item["notice_kind"] == "merge_paused"]


def audited(world, action: str) -> list[str]:
    a = tables.audit
    return [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == action).order_by(a.c.id))]


def red_for(world, minutes: int) -> None:
    """The hub saw the target branch of the repo turn red ``minutes`` ago."""
    b = tables.merge_breakers
    sql(world.db, update(b).values(red_since=func.now() - timedelta(minutes=minutes)).where(b.c.repo == REPO))


# A UAT check that fails


def test_merge_breaker_a_failed_uat_check_trips_it_pauses_the_queue_and_tells_the_admins(world):
    policy(world, jobs={"uat_check": "uat-smoke"}, hold="uat_check")
    first = merged(world, "fix/one", "Fix one")
    second = labeled(world, "fix/two", "Fix two")
    gate_pass(world)
    judge_next(world)
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "manual")])
    gate_pass(world)
    assert breaker(world)["state"] == "armed" and by_iid(world)[2]["state"] == "ready"  # held by !1 (uat_check)
    fail_twice(world, pipeline, "uat-smoke")
    assert gate_pass(world)["tripped"] == 1

    found = breaker(world)
    assert (found["state"], found["cause"], found["trips"]) == ("tripped", "uat_check", 1)
    assert found["reason"].startswith("the UAT check of !1 failed: job uat-smoke of pipeline")
    assert found["tripped_at"] is not None and found["resumed_by"] is None
    entries = by_iid(world)
    assert entries[2]["state"] == "paused" and kinds(entries[2])[-1] == "paused"
    assert entries[2]["reason"].startswith(f"the breaker of {REPO} tripped: it waits for an admin to reset it")
    (notice,) = paused_notices(world)
    assert notice["title"] == f"The merge queue of {REPO} stopped: a UAT check failed"
    assert notice["details"] == {
        "repo": REPO,
        "branch": "dev",
        "cause": "uat_check",
        "causes": ["uat_check", "revert_rate"],  # 1 of 1 merges reverted, before the repo has a baseline
        "paused": [2],
    }
    assert notice["link"] == f"/p/{PROJECT}/merge-queue" and notice["project"] == PROJECT
    assert f"evo-agents hub merge resume {REPO} --project {PROJECT}" in notice["body"]
    assert paused_notices(world, "other") == []  # a writer is not asked
    assert audited(world, "merge.breaker") == [
        f"{PROJECT}/{REPO} cause=uat_check causes=uat_check,revert_rate paused=!2"
    ]

    # The revert of the failure still runs: its gates, its turn, its merge, its check.
    assert world.project.reverted == [(first.sha, "evo-revert/1")]
    green(world, world.project.merge_requests[3], UNDONE)
    gate_pass(world)
    entries = by_iid(world)
    assert entries[3]["state"] == "merged" and entries[1]["state"] == "reverted" and entries[2]["state"] == "paused"
    on_dev(world, world.project.merge_requests[3], jobs=[("uat-smoke", "success")])
    gate_pass(world)
    entries = by_iid(world)
    assert entries[3]["state"] == "done" and entries[2]["state"] == "paused"  # only an admin resets it
    assert breaker(world)["state"] == "tripped" and second.state == "opened"

    assert resume(world, "other").status_code == 403  # a writer
    reset = resume(world)
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert (body["state"], body["resumed_by"], body["resumed"], body["cause"]) == ("armed", "owner", [2], "uat_check")
    assert body["resumed_at"] is not None and body["trips"] == 1
    (notice,) = paused_notices(world)
    assert notice["read_at"] is not None
    assert audited(world, "merge.resume") == [f"{PROJECT}/{REPO} resumed=!2"]
    entry = by_iid(world)[2]
    assert entry["state"] == "ready" and entry["events"][0]["reason"].startswith("owner reset the breaker of")
    gate_pass(world)
    assert by_iid(world)[2]["state"] == "testing" and breaker(world)["state"] == "armed"  # its turn, nothing new
    again = resume(world)
    assert again.status_code == 409 and "has not tripped" in again.json()["message"]


def test_merge_breaker_with_hold_until_merged_a_failed_check_stops_merges_during_the_culprit_search(world):
    policy(world, jobs={"uat_check": "uat-smoke"})
    first, second = merged(world, "fix/one", "Fix one"), merged(world, "fix/two", "Fix two")
    one = on_dev(world, first, jobs=[("uat-smoke", "running")])
    two = on_dev(world, second, jobs=[("uat-smoke", "running")])
    gate_pass(world)
    fail_twice(world, two, "uat-smoke")
    gate_pass(world)
    assert breaker(world)["cause"] == "uat_check" and world.project.reverted == []  # !1's check runs: no culprit yet

    third = labeled(world, "fix/three", "Fix three")
    judged = judge_runs(world)
    gate_pass(world)
    entry = by_iid(world)[3]
    assert entry["state"] == "gating" and entry["reason"] == "waits: the repo's breaker tripped: an admin resets it"
    assert judge_runs(world) == judged and third.state == "opened"  # no Judge, no merge while it is tripped
    fail_twice(world, one, "uat-smoke")
    gate_pass(world)
    entries = by_iid(world)
    assert entries[1]["state"] == "reverting" and world.project.reverted == [(first.sha, "evo-revert/1")]
    assert entries[3]["state"] == "gating" and breaker(world)["trips"] == 1


# The target branch and PROD


def test_merge_breaker_a_target_branch_red_too_long_trips_it_and_no_deploy_starts(world):
    policy(world, jobs=JOBS)
    first = merged(world, "fix/one", "Fix one")
    pipeline = on_dev(world, first)
    pipeline["status"] = "failed"  # dev is red, whoever pushed
    gate_pass(world)
    found = breaker(world)
    assert found["state"] == "armed" and found["red_since"] is not None
    red_for(world, 31)
    ends(world, pipeline, "uat-smoke", "success")
    gate_pass(world)
    found = breaker(world)
    assert (found["state"], found["cause"]) == ("tripped", "target_red")
    assert found["reason"] == "its target branch has been red for 31 minutes, over 30"
    assert by_iid(world)[1]["state"] == "releasing" and played(world) == ["uat-smoke"]
    gate_pass(world)
    assert played(world) == ["uat-smoke"]  # no deploy while it is tripped

    pipeline["status"] = "success"
    reset = resume(world)
    assert reset.status_code == 200 and reset.json()["red_since"] is None and reset.json()["resumed"] == []
    gate_pass(world)
    assert played(world) == ["uat-smoke", "deploy-prod"] and breaker(world)["state"] == "armed"


def test_merge_breaker_a_failed_prod_check_trips_it_and_the_rollback_and_the_revert_still_run(world):
    policy(world, jobs=JOBS)
    first = merged(world, "fix/one", "Fix one")
    one = on_dev(world, first)
    gate_pass(world)
    ends(world, one, "uat-smoke", "success")
    gate_pass(world)
    ends(world, one, "deploy-prod", "success")
    gate_pass(world)
    fail_twice(world, one, "prod-smoke")
    gate_pass(world)  # PROD goes back: the breaker trips before the turn of the same pass
    found = breaker(world)
    assert (found["state"], found["cause"]) == ("tripped", "prod_check")
    assert found["reason"].startswith("the PROD check of !1 failed: job prod-smoke of pipeline")
    gate_pass(world)
    q = tables.merge_queue
    before = sql(world.db, select(q.c.before_sha).where(q.c.iid == 1))[0][0]
    assert played(world)[-1] == f"rollback-prod@{before[:7]}"  # PROD goes back while it is tripped
    ends(world, one, "rollback-prod", "success")
    gate_pass(world)
    assert by_iid(world)[1]["state"] == "reverting" and world.project.reverted == [(first.sha, "evo-revert/1")]
    assert breaker(world)["trips"] == 1 and len(audited(world, "merge.rollback")) == 1


def test_merge_breaker_a_deploy_under_way_when_it_trips_is_not_run_again_and_prod_goes_back(world):
    policy(world, jobs=JOBS)
    first = merged(world, "fix/one", "Fix one")
    one = on_dev(world, first)
    gate_pass(world)
    ends(world, one, "uat-smoke", "success")
    one["status"] = "failed"
    gate_pass(world)  # its deploy starts, and the hub sees dev red
    assert played(world) == ["uat-smoke", "deploy-prod"]
    red_for(world, 45)
    ends(world, one, "deploy-prod", "failed")
    gate_pass(world)
    assert breaker(world)["cause"] == "target_red"
    entry = by_iid(world)[1]
    assert entry["state"] == "rolling_back" and job(world, one, "deploy-prod")["status"] == "failed"  # no retry
    assert entry["reason"].startswith("its prod deploy failed (job deploy-prod of pipeline")


# The counts


def test_merge_breaker_counts_from_its_reset_and_a_revert_gitlab_refuses_after_it_trips_it_again(world):
    policy(world)
    first = merged(world, "fix/one", "Fix one")
    on_dev(world, first, jobs=(), status="success")
    gate_pass(world)
    entry = by_iid(world)[1]
    assert entry["state"] == "done" and breaker(world)["state"] == "armed"
    world.project.revert_refusals[first.sha] = "conflict"
    revert = f"/v1/projects/{PROJECT}/merge-queue/{entry['id']}/revert"
    asked = world.client.post(revert, headers=world.headers["owner"])
    assert asked.status_code == 200, asked.text
    gate_pass(world)  # an admin's revert: 1 of 1 merges reverted, over 10% before a baseline
    found = breaker(world)
    assert (found["state"], found["cause"]) == ("tripped", "revert_rate")
    assert found["reason"] == (
        "1 of its 1 merges of the last 14 days were reverted (100%), over breaker.revert_rate_max 10%, the repo having "
        "no baseline yet"
    )
    assert world.project.reverted == [] and "GitLab would not revert it" in by_iid(world)[1]["reason"]
    assert resume(world).status_code == 200
    gate_pass(world)
    assert breaker(world)["state"] == "armed"  # what happened before the reset trips it no more

    assert world.client.post(revert, headers=world.headers["owner"]).status_code == 200  # an admin tries again
    gate_pass(world)
    found = breaker(world)
    assert (found["state"], found["cause"], found["trips"]) == ("tripped", "revert", 2)
    assert found["reason"].startswith("GitLab would not revert !1: ")
    assert found["reason"].endswith(
        "; 1 of its 1 merges of the last 14 days were reverted (100%), over breaker."
        "revert_rate_max 10%, the repo having no baseline yet"
    )
    notice = paused_notices(world)[0]  # the newest first
    assert notice["details"]["causes"] == ["revert", "revert_rate"] and notice["read_at"] is None


def test_merge_breaker_the_third_pass_of_the_judge_found_wrong_in_seven_days_trips_it(world):
    body = {"repos": {REPO: {"mode": "auto", "target_branch": "dev"}}, "judge": {"worker": "mac-mini"}}
    put_policy(world.client, world.headers["owner"], {**body, "audit": {"sample_rate": 1.0}})
    for branch in ("fix/one", "fix/two", "fix/three"):
        merged(world, branch, branch)
    samples = sorted(view(world)["samples"], key=lambda item: item["id"])
    assert len(samples) == 3
    for sample in samples[:2]:
        answer = world.client.post(
            f"/v1/projects/{PROJECT}/merge-queue/samples/{sample['id']}",
            json={"result": "faulty", "note": "It drops the totals."},
            headers=world.headers["owner"],
        )
        assert answer.status_code == 200, answer.text
    gate_pass(world)
    assert breaker(world)["state"] == "armed"  # two allowed (breaker.judge_misses_7d)
    answer = world.client.post(
        f"/v1/projects/{PROJECT}/merge-queue/samples/{samples[2]['id']}",
        json={"result": "faulty"},
        headers=world.headers["owner"],
    )
    assert answer.status_code == 200, answer.text
    gate_pass(world)
    found = breaker(world)
    assert (found["state"], found["cause"]) == ("tripped", "judge_misses")
    assert found["reason"] == "the Judge passed 3 merges found wrong in 7 days, over 2"


def _merges(world, project_id: int, start: int, rows: list[tuple[str, datetime]]) -> None:
    """Merge requests of the repo the queue merged at each time, ``reverted`` or ``done`` with every check passed."""
    q = tables.merge_queue
    values = [
        {
            "project_id": project_id,
            "repo": REPO,
            "iid": start + index,
            "source": "label",
            "target_branch": "dev",
            "source_branch": f"fix/{start + index}",
            "state": state,
            "merged_at": at,
            "uat_at": at if state == "done" else None,
        }
        for index, (state, at) in enumerate(rows)
    ]
    sql(world.db, insert(q).values(values))


def test_merge_breaker_measures_the_revert_rate_against_the_baseline_and_never_counts_the_merges_of_a_day(world):
    a = tables.autonomy_policies
    sql(world.db, update(a).values(created_at=func.now() - timedelta(days=40)))  # the repo in auto for 40 days
    project_id = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    now = datetime.now(UTC)
    first_days = [("reverted" if day == 0 else "done", now - timedelta(days=39 - day)) for day in range(10)]
    _merges(world, project_id, 100, first_days)  # the baseline: 1 of 10 reverted, 10%
    pairs = []
    for index in range(7):  # 7 reverted, each followed by a merge that passed: none in a row
        at = now - timedelta(days=5, minutes=-2 * index)
        pairs += [("reverted", at), ("done", at + timedelta(minutes=1))]
    _merges(world, project_id, 200, pairs)
    _merges(world, project_id, 300, [("done", now - timedelta(minutes=5 + index)) for index in range(22)])  # a day
    gate_pass(world)
    assert breaker(world)["state"] == "armed"  # 7 of 36 is 19%, under 2 x 10%; 36 merges trip nothing
    at = now - timedelta(days=4)
    _merges(world, project_id, 400, [("reverted", at), ("done", at + timedelta(minutes=1))])
    gate_pass(world)
    found = breaker(world)
    assert (found["state"], found["cause"]) == ("tripped", "revert_rate")
    assert found["reason"] == (
        "8 of its 38 merges of the last 14 days were reverted (21%), over breaker.revert_rate_factor 2 times the "
        "repo's baseline 10%"
    )


# The command line


def test_merge_breaker_from_the_command_line(world, hub_db, tmp_path, github, monkeypatch, capsys):
    from tests.hub.test_run_stream import serving

    project_id = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    b, q = tables.merge_breakers, tables.merge_queue

    def tripped() -> None:
        values = {"state": "tripped", "cause": "rollback", "reason": "the hub rolled PROD back for !7"}
        found = sql(world.db, select(func.count()).select_from(b))[0][0]
        if found:
            sql(world.db, update(b).values(tripped_at=func.now(), **values))
        else:
            sql(world.db, insert(b).values(project_id=project_id, repo=REPO, tripped_at=func.now(), trips=1, **values))
        sql(world.db, update(q).values(state="paused", reason="the breaker tripped").where(q.c.iid == 7))

    row = {"project_id": project_id, "repo": REPO, "source": "label", "target_branch": "dev"}
    sql(world.db, insert(q).values({**row, "iid": 7, "source_branch": "fix/seven", "title": "Fix seven"}))
    tripped()
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        homes = {
            login: write_credentials(tmp_path / login, url, login, world.headers[login]["Authorization"].split()[1])
            for login in ("owner", "other")
        }

        def merge(who: str, *args: str):
            return run_cli(monkeypatch, capsys, homes[who], "hub", "merge", *args, "--project", PROJECT)

        listed = ok(merge("other", "list")).out.splitlines()
        assert listed[0].startswith(f"Breaker of {REPO}: tripped at ") and listed[0].endswith(
            "UTC (rollback): the hub rolled PROD back for !7"
        )
        assert listed[1] == f"  an admin resets it: evo-agents hub merge resume {REPO} --project {PROJECT}"
        assert listed[2].split() == ["MR", "REPO", "SOURCE", "STATE", "WHY"]
        assert listed[3].split()[:4] == ["!7", REPO, "label", "paused"]
        assert_json_keys("hub merge list", as_json(merge("other", "list", "--repo", REPO, "--json")))
        refused = merge("other", "resume", REPO)
        assert refused.code == 1 and "needs the admin role on project" in refused.err
        assert ok(merge("owner", "resume", REPO)).out == (
            f"Reset the breaker of {REPO} in project {PROJECT}: !7 wait for their turn again.\n"
        )
        assert ok(merge("owner", "list")).out.splitlines()[0] == f"Breaker of {REPO}: armed"
        tripped()
        reset = as_json(merge("owner", "resume", REPO, "--json"))
        assert_json_keys("hub merge resume", reset)
        assert reset["resumed"] == [7] and reset["state"] == "armed"
        again = merge("owner", "resume", REPO)
        assert again.code == 1 and "has not tripped" in again.err
